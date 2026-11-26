from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  owner TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '启用' CHECK(status IN ('启用','停用'))
);
CREATE TABLE IF NOT EXISTS environments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  sort_order INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS app_env_versions (
  app_id INTEGER NOT NULL,
  env_id INTEGER NOT NULL,
  version TEXT NOT NULL DEFAULT '',
  updated_at TEXT,
  PRIMARY KEY(app_id, env_id),
  FOREIGN KEY(app_id) REFERENCES apps(id),
  FOREIGN KEY(env_id) REFERENCES environments(id)
);
CREATE TABLE IF NOT EXISTS releases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  serial TEXT NOT NULL UNIQUE,
  app_id INTEGER NOT NULL,
  target_version TEXT NOT NULL,
  artifact_digest TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '待入闸'
    CHECK(status IN ('待入闸','已入闸','晋升中','已放行','回滚中','已回滚','已关闭')),
  pipeline_status TEXT NOT NULL DEFAULT '未回执'
    CHECK(pipeline_status IN ('未回执','绿','红')),
  current_env_id INTEGER,
  canary_ratio INTEGER NOT NULL DEFAULT 10,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  admitted_at TEXT,
  released_at TEXT,
  rolled_back_at TEXT,
  rollback_version TEXT,
  FOREIGN KEY(app_id) REFERENCES apps(id),
  FOREIGN KEY(current_env_id) REFERENCES environments(id)
);
CREATE TABLE IF NOT EXISTS promote_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  release_id INTEGER NOT NULL,
  from_env_id INTEGER,
  to_env_id INTEGER NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN ('放行','拒绝')),
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(release_id) REFERENCES releases(id),
  FOREIGN KEY(from_env_id) REFERENCES environments(id),
  FOREIGN KEY(to_env_id) REFERENCES environments(id)
);
CREATE TABLE IF NOT EXISTS release_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  env_id INTEGER NOT NULL,
  open_at TEXT NOT NULL,
  close_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '窗开' CHECK(status IN ('窗开','窗关')),
  FOREIGN KEY(env_id) REFERENCES environments(id)
);
CREATE TABLE IF NOT EXISTS rollback_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  env_id INTEGER NOT NULL,
  open_at TEXT NOT NULL,
  close_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '窗开' CHECK(status IN ('窗开','窗关')),
  FOREIGN KEY(env_id) REFERENCES environments(id)
);
CREATE TABLE IF NOT EXISTS instances (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id INTEGER NOT NULL,
  env_id INTEGER NOT NULL,
  version TEXT NOT NULL,
  health TEXT NOT NULL DEFAULT '探针绿' CHECK(health IN ('探针绿','探针红','探针待报')),
  release_id INTEGER,
  updated_at TEXT NOT NULL,
  fail_count INTEGER NOT NULL DEFAULT 0,
  backfilled INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(app_id) REFERENCES apps(id),
  FOREIGN KEY(env_id) REFERENCES environments(id),
  FOREIGN KEY(release_id) REFERENCES releases(id)
);
CREATE TABLE IF NOT EXISTS switch_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  switch_key TEXT NOT NULL UNIQUE,
  release_id INTEGER NOT NULL,
  app_id INTEGER NOT NULL,
  env_id INTEGER NOT NULL,
  version TEXT NOT NULL,
  roster_committed INTEGER NOT NULL DEFAULT 1,
  attached_count INTEGER NOT NULL DEFAULT 1,
  outcome TEXT NOT NULL DEFAULT '',
  gap_mark TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT '已切',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  replayed_count INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(release_id) REFERENCES releases(id),
  FOREIGN KEY(app_id) REFERENCES apps(id),
  FOREIGN KEY(env_id) REFERENCES environments(id)
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  ref_id INTEGER,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
"""

RELEASE_STATUSES = ("待入闸", "已入闸", "晋升中", "已放行", "回滚中", "已回滚", "已关闭")
PIPELINE_STATUSES = ("未回执", "绿", "红")
HEALTH_STATUSES = ("探针绿", "探针红", "探针待报")
WINDOW_STATUSES = ("窗开", "窗关")

# 进程级“锚连接”：WAL 模式下最后一个连接关闭会触发 checkpoint+fsync，
# 在慢盘（overlay/WSL）上每次请求开关连接会产生数百毫秒卡顿。保留一个
# 常驻连接后，请求连接关闭不再充当“最后一个连接”，提交延迟恢复正常。
_anchor: sqlite3.Connection | None = None
_anchor_path: str | None = None


def retain_anchor() -> None:
    global _anchor, _anchor_path
    path = str(DB_PATH)
    if _anchor is not None and _anchor_path == path:
        return
    if _anchor is not None:
        try:
            _anchor.close()
        except sqlite3.Error:
            pass
    _anchor = connect()
    _anchor_path = path


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    # WAL 下 NORMAL 足够安全且不会每次提交都 fsync，显著降低提交延迟。
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate_db(conn: sqlite3.Connection) -> None:
    """Idempotent additive migration for snapshots created before the
    fail_count / backfilled / 探针待报 / switch_ledger era."""
    cols = _columns(conn, "instances")
    if "fail_count" not in cols:
        conn.execute("ALTER TABLE instances ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0")
    if "backfilled" not in cols:
        conn.execute("ALTER TABLE instances ADD COLUMN backfilled INTEGER NOT NULL DEFAULT 0")
    schema_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='instances'"
    ).fetchone()
    if schema_sql and "探针待报" not in (schema_sql[0] or ""):
        conn.execute("ALTER TABLE instances RENAME TO instances_old")
        conn.execute(
            """
            CREATE TABLE instances (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              app_id INTEGER NOT NULL,
              env_id INTEGER NOT NULL,
              version TEXT NOT NULL,
              health TEXT NOT NULL DEFAULT '探针绿'
                CHECK(health IN ('探针绿','探针红','探针待报')),
              release_id INTEGER,
              updated_at TEXT NOT NULL,
              fail_count INTEGER NOT NULL DEFAULT 0,
              backfilled INTEGER NOT NULL DEFAULT 0,
              FOREIGN KEY(app_id) REFERENCES apps(id),
              FOREIGN KEY(env_id) REFERENCES environments(id),
              FOREIGN KEY(release_id) REFERENCES releases(id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO instances
              (id, app_id, env_id, version, health, release_id, updated_at, fail_count, backfilled)
            SELECT id, app_id, env_id, version, health, release_id, updated_at,
                   CASE WHEN health='探针红' AND COALESCE(fail_count, 0) < 1
                        THEN 1 ELSE COALESCE(fail_count, 0) END,
                   COALESCE(backfilled, 0)
            FROM instances_old
            """
        )
        conn.execute("DROP TABLE instances_old")
    # 回滚目标版本独立成列：不得改写 target_version，否则发版详情「目标版本」
    # 与名册当前列会长期双账。
    rel_cols = _columns(conn, "releases")
    if "rollback_version" not in rel_cols:
        conn.execute("ALTER TABLE releases ADD COLUMN rollback_version TEXT")
    ledger_cols = _columns(conn, "switch_ledger")
    if "state" not in ledger_cols:
        conn.execute(
            "ALTER TABLE switch_ledger ADD COLUMN state TEXT NOT NULL DEFAULT '已切'"
        )
    conn.commit()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        conn.executescript(SCHEMA)
        migrate_db(conn)
    retain_anchor()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]
