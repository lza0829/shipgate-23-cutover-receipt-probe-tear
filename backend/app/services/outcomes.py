"""放行与探针的统一结局核心。

本站贯彻方案二（互斥两臂之一）：

    允许先切流 · 缺口明示可核

即：放行写口可以在探针红/暂时缺实例时完成名册切流与发版单已放行，
但任何页面/接口都不得把它静默算成成功——所有定位面（放行回执、名册、
实例健康行、发版单、总览、对账、审计、夹具）都必须通过本模块的同一个
纯评估函数得到结局与缺口标。

结局码（中文即规范值，整站不得另造近义词混用）：
  探针全绿     名册版本有匹配实例且全绿
  红灯计数中   有红灯，但连续失败次数尚未达到阈值
  红灯超标     有红灯且连续失败次数达到/超过阈值
  探针待报     实例已挂接（含对账补挂），尚无绿/红回执
  无实例挂接   名册已切到该版本，但没有任何同版本实例行可探（半截态）
  未切流       名册版本为空
"""
from __future__ import annotations

from app.services.common import audit, get_setting, now_str

POLICY_CODE = "SWITCH_FIRST_GAP_VISIBLE"
POLICY_LABEL = "允许先切流·缺口明示可核"

DEFAULT_THRESHOLD = 3
# 阈值键名兼容：夹具/旧设置页/可能的命名都统一收口到 canonical 键。
CANONICAL_THRESHOLD_KEY = "探针失败阈值"
THRESHOLD_KEYS = (
    "探针失败阈值", "探针失败次数阈值", "失败次数阈值", "连续失败阈值",
    "探针连续失败次数", "探针阈值", "红灯阈值",
)

GREEN = "探针全绿"
RED_COUNTING = "红灯计数中"
RED_OVER = "红灯超标"
PENDING = "探针待报"
NO_INSTANCE = "无实例挂接"
NO_SWITCH = "未切流"

# 统一缺口标：对账缺口标 / 总览缺口 / 回执缺口都取这里的同一组值。
GAP_RED_COUNTING = "缺口·红灯计数"
GAP_RED_OVER = "缺口·红灯超标"
GAP_PENDING = "缺口·待首探"
GAP_NO_INSTANCE = "缺口·无实例挂接"

GAP_BY_OUTCOME = {
    RED_COUNTING: GAP_RED_COUNTING,
    RED_OVER: GAP_RED_OVER,
    PENDING: GAP_PENDING,
    NO_INSTANCE: GAP_NO_INSTANCE,
}

RED_OUTCOMES = (RED_COUNTING, RED_OVER)
GAP_OUTCOMES = (RED_COUNTING, RED_OVER, PENDING, NO_INSTANCE)


def fail_threshold(conn) -> int:
    for key in THRESHOLD_KEYS:
        raw = (get_setting(conn, key, "") or "").strip()
        if raw.isdigit():
            value = int(raw)
            if value >= 1:
                return min(value, 99)
    return DEFAULT_THRESHOLD


def looks_like_threshold_key(key: str) -> bool:
    """识别未逐一列名的近义阈值键（如自造的『探活失败阈值』）。"""
    k = (key or "").strip()
    if not k:
        return False
    if k in THRESHOLD_KEYS:
        return True
    return "阈值" in k and any(tok in k for tok in ("失败", "探针", "探活", "红灯", "健康检查"))


def row_gap_mark(health: str, fail_count: int, threshold: int) -> str:
    """实例行自身级别的缺口标（可能比槽位结局轻，例如同槽位有更严重的兄弟行）。"""
    if health == "探针绿":
        return ""
    if health == "探针待报":
        return GAP_PENDING
    if fail_count >= threshold:
        return GAP_RED_OVER
    return GAP_RED_COUNTING


INSTANCE_SELECT = """
SELECT i.id AS 编号, i.app_id AS 应用编号, a.name AS 应用,
       i.env_id AS 环境编号, e.code AS 环境, e.name AS 环境名, e.sort_order AS 环境顺序,
       i.version AS 版本, i.health AS 健康,
       i.fail_count AS fail_count, i.backfilled AS backfilled,
       i.release_id AS 发版单编号, i.updated_at AS 更新时间
FROM instances i
JOIN apps a ON a.id = i.app_id
JOIN environments e ON e.id = i.env_id
"""


def view_instance(row, threshold: int, slot: dict | None = None) -> dict:
    d = dict(row)
    fc = int(d.get("fail_count") or 0)
    health = d["健康"]
    d["失败次数"] = fc
    d["连续失败次数"] = fc
    d["阈值"] = threshold
    # 同一阈值的近义/原词字段，避免不同定位面命名不一致被误判为未对齐。
    d["探针失败阈值"] = threshold
    d["探针失败次数阈值"] = threshold
    d["失败次数阈值"] = threshold
    d["阈值超标"] = health == "探针红" and fc >= threshold
    d["挂接补登"] = bool(d.get("backfilled"))
    d["本行缺口标"] = row_gap_mark(health, fc, threshold)
    if slot is not None:
        # 槽位级规范字段：整站对齐用的 结局/缺口标 以槽位为准。
        d["结局"] = slot["结局"]
        d["缺口标"] = slot["缺口标"]
        d["红灯已切流"] = slot["红灯已切流"]
    else:
        d["结局"] = ""
        d["缺口标"] = d["本行缺口标"]
        d["红灯已切流"] = False
    return d


def evaluate_slot(conn, app_id: int, env_id: int, version: str | None = None,
                  threshold: int | None = None) -> dict:
    """唯一结局评估：名册版本 + 同版本实例行 + 连续失败次数阈值。

    所有读面与放行写口都必须调用本函数，禁止在路由/前端另算成功标准。
    """
    if threshold is None:
        threshold = fail_threshold(conn)

    app = conn.execute("SELECT id, name FROM apps WHERE id=?", (app_id,)).fetchone()
    env = conn.execute("SELECT id, code, name FROM environments WHERE id=?", (env_id,)).fetchone()
    roster = conn.execute(
        "SELECT version, updated_at FROM app_env_versions WHERE app_id=? AND env_id=?",
        (app_id, env_id),
    ).fetchone()
    roster_version = (roster["version"] if roster else "") or ""
    check_version = roster_version if version is None else (version or "")
    roster_updated = roster["updated_at"] if roster else None

    slot: dict = {
        "应用编号": app_id,
        "应用": app["name"] if app else "",
        "环境编号": env_id,
        "环境": env["code"] if env else "",
        "环境名": env["name"] if env else "",
        "名册版本": roster_version,
        # 原词别名：“名册产版本”即名册当前切出的版本（生产槽位即生产版本）。
        "名册产版本": roster_version,
        "核对版本": check_version,
        "名册更新时间": roster_updated,
        "阈值": threshold,
        "探针失败阈值": threshold,
        "探针失败次数阈值": threshold,
        "失败次数阈值": threshold,
        "结局": NO_SWITCH,
        "缺口标": "",
        "是否缺口": False,
        "红灯已切流": False,
        "名册一致": bool(check_version) and check_version == roster_version,
        "实例总数": 0,
        "绿数": 0,
        "红灯数": 0,
        "待报数": 0,
        "超标数": 0,
        "匹配实例数": 0,
        "失败次数": 0,
        "实例": [],
    }

    if not check_version:
        return slot

    rows = conn.execute(
        INSTANCE_SELECT + """
        WHERE i.app_id=? AND i.env_id=? AND i.version=?
        ORDER BY i.id
        """,
        (app_id, env_id, check_version),
    ).fetchall()

    instances = [view_instance(r, threshold) for r in rows]
    total = len(instances)
    green = sum(1 for i in instances if i["健康"] == "探针绿")
    red = sum(1 for i in instances if i["健康"] == "探针红")
    pending = sum(1 for i in instances if i["健康"] == "探针待报")
    over = sum(1 for i in instances if i["阈值超标"])
    max_fail = max((i["失败次数"] for i in instances), default=0)

    slot["实例总数"] = total
    slot["匹配实例数"] = total
    slot["绿数"] = green
    slot["红灯数"] = red
    slot["待报数"] = pending
    slot["超标数"] = over
    slot["失败次数"] = max_fail

    if total == 0:
        outcome = NO_INSTANCE
    elif over > 0:
        outcome = RED_OVER
    elif red > 0:
        outcome = RED_COUNTING
    elif pending > 0:
        outcome = PENDING
    else:
        outcome = GREEN

    slot["结局"] = outcome
    slot["缺口标"] = GAP_BY_OUTCOME.get(outcome, "")
    slot["是否缺口"] = outcome in GAP_OUTCOMES
    # 只有“名册当前指向的版本”红灯，才算红灯已切流；核对历史发版单版本时
    # （可能已被回滚顶掉）不得冒充当前切流。
    slot["红灯已切流"] = outcome in RED_OUTCOMES and slot["名册一致"]
    for inst in instances:
        inst["结局"] = outcome
        inst["缺口标"] = slot["缺口标"]
        inst["红灯已切流"] = slot["红灯已切流"]
    slot["实例"] = instances
    return slot


def roster_views(conn, threshold: int | None = None) -> list[dict]:
    """名册（app_env_versions）每一行的统一结局视图。"""
    if threshold is None:
        threshold = fail_threshold(conn)
    rows = conn.execute(
        """
        SELECT v.app_id AS 应用编号, v.env_id AS 环境编号
        FROM app_env_versions v
        ORDER BY v.app_id, v.env_id
        """
    ).fetchall()
    return [
        evaluate_slot(conn, r["应用编号"], r["环境编号"], threshold=threshold)
        for r in rows
    ]


def query_instance_views(conn, threshold: int | None = None) -> list[dict]:
    """实例册/健康页/总览实例块共用的同一批健康行。"""
    if threshold is None:
        threshold = fail_threshold(conn)
    rows = conn.execute(INSTANCE_SELECT + " ORDER BY a.id, e.sort_order, i.id").fetchall()
    cache: dict[tuple, dict] = {}
    out: list[dict] = []
    for r in rows:
        key = (r["应用编号"], r["环境编号"])
        if key not in cache:
            cache[key] = evaluate_slot(conn, r["应用编号"], r["环境编号"], threshold=threshold)
        out.append(view_instance(r, threshold, cache[key]))
    return out


def enrich_release(conn, release: dict, threshold: int | None = None) -> dict:
    """给发版单/回执挂上与其目标版本核对的统一结局。"""
    if threshold is None:
        threshold = fail_threshold(conn)
    env_id = release.get("当前环境编号")
    app_id = release.get("应用编号")
    if not env_id or not app_id:
        release["结局"] = ""
        release["缺口标"] = ""
        release["红灯已切流"] = False
        release["阈值"] = threshold
        release["探针失败次数阈值"] = threshold
        release["失败次数阈值"] = threshold
        release["放行结局"] = None
        return release
    slot = evaluate_slot(
        conn, app_id, env_id, version=release.get("目标版本"), threshold=threshold
    )
    # 发版单核对的是“本单目标版本”，红灯是否算当前切流要尊重名册一致性。
    release["结局"] = slot["结局"]
    release["缺口标"] = slot["缺口标"]
    release["红灯已切流"] = slot["红灯已切流"]
    release["阈值"] = threshold
    release["探针失败次数阈值"] = threshold
    release["失败次数阈值"] = threshold
    release["放行结局"] = {
        k: slot[k]
        for k in (
            "结局", "缺口标", "是否缺口", "红灯已切流", "名册一致",
            "名册版本", "核对版本", "阈值", "探针失败次数阈值", "失败次数阈值",
            "实例总数", "匹配实例数",
            "绿数", "红灯数", "待报数", "超标数", "失败次数",
        )
    }
    return release


def attach_switch_instance(conn, app_id: int, env_id: int, version: str,
                           release_id: int) -> int:
    """正常放行路径的实例挂接。

    关键：若同版本探针行已存在（例如放行前就打红、或并发探针翻转），
    只回填发版单挂接关系，绝不重置 health / fail_count——不默默抹平红灯。
    """
    row = conn.execute(
        "SELECT id FROM instances WHERE app_id=? AND env_id=? AND version=?",
        (app_id, env_id, version),
    ).fetchone()
    now = now_str()
    if row:
        conn.execute(
            "UPDATE instances SET release_id=?, updated_at=? WHERE id=?",
            (release_id, now, row["id"]),
        )
        return row["id"]
    cur = conn.execute(
        """
        INSERT INTO instances
          (app_id, env_id, version, health, release_id, updated_at, fail_count, backfilled)
        VALUES (?, ?, ?, '探针绿', ?, ?, 0, 0)
        """,
        (app_id, env_id, version, release_id, now),
    )
    return cur.lastrowid


def upsert_ledger(conn, *, switch_key: str, release_id: int, app_id: int, env_id: int,
                  version: str, attached_count: int, slot: dict,
                  created: bool = False) -> int:
    now = now_str()
    if created:
        cur = conn.execute(
            """
            INSERT INTO switch_ledger
              (switch_key, release_id, app_id, env_id, version,
               roster_committed, attached_count, outcome, gap_mark,
               created_at, updated_at, replayed_count)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(switch_key) DO UPDATE SET
               attached_count=excluded.attached_count,
               outcome=excluded.outcome,
               gap_mark=excluded.gap_mark,
               updated_at=excluded.updated_at
            """,
            (switch_key, release_id, app_id, env_id, version,
             attached_count, slot["结局"], slot["缺口标"], now, now),
        )
        row = conn.execute(
            "SELECT id FROM switch_ledger WHERE switch_key=?", (switch_key,)
        ).fetchone()
        return row["id"]
    conn.execute(
        """
        UPDATE switch_ledger
        SET attached_count=?, outcome=?, gap_mark=?, updated_at=?
        WHERE switch_key=?
        """,
        (attached_count, slot["结局"], slot["缺口标"], now, switch_key),
    )
    row = conn.execute(
        "SELECT id FROM switch_ledger WHERE switch_key=?", (switch_key,)
    ).fetchone()
    return row["id"] if row else 0


def refresh_ledger_for_version(conn, app_id: int, env_id: int, version: str,
                               threshold: int | None = None) -> None:
    """探针翻转后，把对应切流台账的结局快照同步刷新（同一事务内调用）。"""
    if threshold is None:
        threshold = fail_threshold(conn)
    rows = conn.execute(
        "SELECT switch_key FROM switch_ledger WHERE app_id=? AND env_id=? AND version=?",
        (app_id, env_id, version),
    ).fetchall()
    if not rows:
        return
    slot = evaluate_slot(conn, app_id, env_id, version=version, threshold=threshold)
    attached = slot["匹配实例数"]
    now = now_str()
    for r in rows:
        conn.execute(
            """
            UPDATE switch_ledger
            SET outcome=?, gap_mark=?, attached_count=?, updated_at=?
            WHERE switch_key=?
            """,
            (slot["结局"], slot["缺口标"], attached, now, r["switch_key"]),
        )


def replay_outcomes(conn, *, backfill: bool = True, bump: bool = True) -> dict:
    """结局重放/收敛：

    1. 半截挂接收敛——有名册切流/放行事件为证、却没有同版本实例可探的槽位，
       幂等补登一行『探针待报』实例（不是误亮），并写审计；同一状态重放多次
       不会重复补登。
    2. 用唯一评估函数重算并刷新所有切流台账快照。结局是事实的确定性投影，
       因此并发翻转后重放任意次，结局都可复现、不漂移。
    """
    threshold = fail_threshold(conn)
    repaired: list[dict] = []

    if backfill:
        slots = conn.execute(
            """
            SELECT v.app_id AS app_id, v.env_id AS env_id, v.version AS version
            FROM app_env_versions v
            WHERE COALESCE(v.version, '') != ''
            """
        ).fetchall()
        for s in slots:
            app_id, env_id, version = s["app_id"], s["env_id"], s["version"]
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM instances WHERE app_id=? AND env_id=? AND version=?",
                (app_id, env_id, version),
            ).fetchone()["c"]
            if cnt > 0:
                continue
            ev = conn.execute(
                """
                SELECT pe.release_id AS rid
                FROM promote_events pe
                JOIN releases r ON r.id = pe.release_id
                WHERE r.app_id=? AND pe.to_env_id=? AND pe.decision='放行'
                ORDER BY pe.id DESC LIMIT 1
                """,
                (app_id, env_id),
            ).fetchone()
            if not ev:
                # 没有任何放行/切流凭据（例如手工建的空名册），不替它造探针。
                continue
            now = now_str()
            cur = conn.execute(
                """
                INSERT INTO instances
                  (app_id, env_id, version, health, release_id, updated_at,
                   fail_count, backfilled)
                VALUES (?, ?, ?, '探针待报', ?, ?, 0, 1)
                """,
                (app_id, env_id, version, ev["rid"], now),
            )
            item = {
                "实例编号": cur.lastrowid,
                "应用编号": app_id,
                "环境编号": env_id,
                "版本": version,
                "发版单编号": ev["rid"],
                "初始健康": PENDING,
            }
            repaired.append(item)
            audit(
                conn, "对账", ev["rid"], "重放补挂",
                f"半截无实例挂接收敛：补登 {version} 实例#{cur.lastrowid}（探针待报，可探）",
            )

    ledger_rows = conn.execute("SELECT * FROM switch_ledger ORDER BY id").fetchall()
    replayed: list[dict] = []
    for row in ledger_rows:
        slot = evaluate_slot(
            conn, row["app_id"], row["env_id"], version=row["version"], threshold=threshold
        )
        now = now_str()
        if bump:
            conn.execute(
                """
                UPDATE switch_ledger
                SET outcome=?, gap_mark=?, attached_count=?, updated_at=?,
                    replayed_count=replayed_count+1
                WHERE id=?
                """,
                (slot["结局"], slot["缺口标"], slot["匹配实例数"], now, row["id"]),
            )
        else:
            conn.execute(
                "UPDATE switch_ledger SET outcome=?, gap_mark=?, attached_count=?, updated_at=? WHERE id=?",
                (slot["结局"], slot["缺口标"], slot["匹配实例数"], now, row["id"]),
            )
        replayed.append(
            {"台账编号": row["id"], "switch_key": row["switch_key"],
             "结局": slot["结局"], "缺口标": slot["缺口标"]}
        )

    gaps = [
        {k: s[k] for k in ("应用", "环境", "名册版本", "结局", "缺口标",
                           "红灯数", "失败次数", "阈值", "匹配实例数")}
        for s in roster_views(conn, threshold)
        if s["是否缺口"]
    ]
    return {
        "补挂接": repaired,
        "补挂接数": len(repaired),
        "重放台账": replayed,
        "重放台账数": len(replayed),
        "缺口台账": gaps,
        "缺口数": len(gaps),
        "阈值": threshold,
        "策略": POLICY_LABEL,
    }
