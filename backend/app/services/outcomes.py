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

# 成功灯的证据缺口：探针/名册/事件/审计四件缺一整笔时必须退回，不得亮成功灯。
GAP_EVIDENCE = "缺口·证据不全"
GAP_ROSTER_NOT_SWITCHED = "缺口·名册未切"

# 切流状态（成功灯）：只有四件同核才允许亮「切流成功」。
CUTOVER_NONE = "未切流"
CUTOVER_BASELINE = "基线"          # 名册有版本但从未经闸门切流（种子/手工名册）
CUTOVER_OK = "切流成功"
CUTOVER_FROZEN = "冻结已切"        # 切流记录在，但证据不全/探针未过 → 不得算已切
CUTOVER_ROSTER_MISSING = "名册未切"  # 探针/事件/审计在，名册当前列尚未切
CUTOVER_ROLLED_BACK = "已回滚"      # 回滚旁路收束，前向切流不得再亮成功灯

EVIDENCE_KEYS = ("探针", "名册", "事件", "审计")

PROD_ENV_CODE = "产"

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


def _env_code(conn, env_id: int) -> str:
    row = conn.execute("SELECT code FROM environments WHERE id=?", (env_id,)).fetchone()
    return row["code"] if row else ""


def _probe_state(conn, app_id: int, env_id: int, version: str, threshold: int) -> dict:
    """核对指定版本的探针是否过线：有同版本实例且全部探针绿。"""
    rows = conn.execute(
        INSTANCE_SELECT + " WHERE i.app_id=? AND i.env_id=? AND i.version=?",
        (app_id, env_id, version),
    ).fetchall()
    total = len(rows)
    green = sum(1 for r in rows if r["健康"] == "探针绿")
    passed = total > 0 and green == total
    return {"实例总数": total, "绿数": green, "过线": passed}


def _latest_forward_event(conn, app_id: int, env_id: int, version: str | None = None):
    """最近一次『放行』切流事件；version 给定时只认目标版本与之一致的单。"""
    sql = """
        SELECT pe.id AS event_id, pe.release_id AS rid, pe.created_at AS at,
               r.target_version AS target, r.status AS status,
               r.rollback_version AS rollback_version
        FROM promote_events pe JOIN releases r ON r.id = pe.release_id
        WHERE r.app_id=? AND pe.to_env_id=? AND pe.decision='放行'
    """
    params: list = [app_id, env_id]
    if version is not None:
        sql += " AND r.target_version=?"
        params.append(version)
    sql += " ORDER BY pe.id DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def _has_switch_audit(conn, release_id: int, version: str) -> bool:
    row = conn.execute(
        """
        SELECT id FROM audit_events
        WHERE kind='切流' AND ref_id=? AND note LIKE ?
        ORDER BY id DESC LIMIT 1
        """,
        (release_id, f"%名册产版本={version}%"),
    ).fetchone()
    return row is not None


def cutover_evidence(conn, app_id: int, env_id: int, version: str | None = None,
                     threshold: int | None = None, slot: dict | None = None) -> dict:
    """切流成功灯的单一证据核心：探针 ∪ 名册 ∪ 事件 ∪ 审计 必须同核。

    成功灯只能由「健康探针过线 ∧ 名册环境列已切 ∧ 切流事件同核 ∧ 审计同核」
    重放得出；缺一整笔就退回（冻结已切），且切流桶不得计为已切。
    只对生产环境（产）计成功灯，避免非生产晋升噪声混入主链。
    """
    if threshold is None:
        threshold = fail_threshold(conn)
    if slot is None:
        slot = evaluate_slot(conn, app_id, env_id, version=version, threshold=threshold)
    roster_version = (slot.get("名册版本") or "")
    env_code = _env_code(conn, env_id)
    applicable = env_code == PROD_ENV_CODE

    base = {
        "适用": applicable,
        "名册已切": False,
        "探针过线": False,
        "切流事件": False,
        "审计切流": False,
        "证据": {k: False for k in EVIDENCE_KEYS},
        "缺证": list(EVIDENCE_KEYS),
        "切流状态": CUTOVER_NONE,
        "切流成功": False,
        "证据缺口标": "",
        "切流目标版本": "",
        "切流核对版本": "",
        "切流发版单编号": None,
        "已回滚": False,
    }
    if not applicable:
        base["切流状态"] = CUTOVER_BASELINE if roster_version else CUTOVER_NONE
        base["缺证"] = []
        return base

    intent = _latest_forward_event(conn, app_id, env_id, version)
    if not intent:
        # 核对版本与事件目标不符（典型是回滚后名册指向旧版本）：若最近一次
        # 前向切流已回滚，本槽位即为『已回滚』，不得退回基线冒充成功。
        latest = _latest_forward_event(conn, app_id, env_id, None)
        if latest and latest["status"] == "已回滚":
            intent = latest
        else:
            # 名册有版本但从未经过闸门（种子/手工名册）：基线，不冒充失败。
            base["切流状态"] = CUTOVER_BASELINE if roster_version else CUTOVER_NONE
            base["缺证"] = []
            return base

    target = intent["target"] or ""
    rolled_back = intent["status"] == "已回滚"
    # 回滚后名册应指向回滚版本；探针与名册都按该版本核对。
    compare_version = (intent["rollback_version"] or target) if rolled_back else target
    roster_switched = bool(compare_version) and roster_version == compare_version
    probe = _probe_state(conn, app_id, env_id, compare_version, threshold)
    event_ok = True  # intent 本身就是一条『放行』切流事件
    audit_ok = _has_switch_audit(conn, intent["rid"], target)

    base.update({
        "名册已切": roster_switched,
        "探针过线": probe["过线"],
        "切流事件": event_ok,
        "审计切流": audit_ok,
        "切流目标版本": target,
        "切流核对版本": compare_version,
        "切流发版单编号": intent["rid"],
        "已回滚": rolled_back,
        "证据": {
            "探针": probe["过线"],
            "名册": roster_switched,
            "事件": event_ok,
            "审计": audit_ok,
        },
    })
    base["缺证"] = [k for k in EVIDENCE_KEYS if not base["证据"][k]]

    if rolled_back:
        state = CUTOVER_ROLLED_BACK
    elif not roster_switched:
        state = CUTOVER_ROSTER_MISSING
    elif probe["过线"] and event_ok and audit_ok:
        state = CUTOVER_OK
    else:
        state = CUTOVER_FROZEN

    base["切流状态"] = state
    base["切流成功"] = state == CUTOVER_OK
    if state == CUTOVER_ROSTER_MISSING:
        base["证据缺口标"] = GAP_ROSTER_NOT_SWITCHED
    elif state == CUTOVER_FROZEN and (not event_ok or not audit_ok):
        base["证据缺口标"] = GAP_EVIDENCE
    return base


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
    # 行级结局必须按本行自己的版本核对；同槽位有旧版本残留（回滚旁路/跨环旁读）
    # 时不得借用槽位缓存把它标成『探针全绿』。
    row_outcome = ""
    if health == "探针绿":
        row_outcome = GREEN
    elif health == "探针待报":
        row_outcome = PENDING
    elif fc >= threshold:
        row_outcome = RED_OVER
    else:
        row_outcome = RED_COUNTING
    if slot is not None:
        verified = row_outcome
        roster_version = slot.get("名册版本") or ""
        check_version = slot.get("核对版本") or roster_version
        matches_slot = d.get("版本") == (check_version or roster_version)
        d["名册版本一致"] = bool(matches_slot)
        if matches_slot:
            # 槽位级规范字段：整站对齐用的 结局/缺口标 以槽位为准。
            verified = slot["结局"]
            d["缺口标"] = slot["缺口标"]
            d["红灯已切流"] = slot["红灯已切流"]
            d["切流状态"] = slot.get("切流状态", "")
            d["切流成功"] = bool(slot.get("切流成功"))
        else:
            # 跨环/残留旁读：只按本行自身证据，且明示与本槽名册版本不符。
            d["缺口标"] = d["本行缺口标"]
            d["红灯已切流"] = False
            d["切流状态"] = ""
            d["切流成功"] = False
        d["结局"] = verified
    else:
        d["结局"] = row_outcome
        d["缺口标"] = d["本行缺口标"]
        d["红灯已切流"] = False
        d["名册版本一致"] = False
        d["切流状态"] = ""
        d["切流成功"] = False
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
    roster_updated = roster["updated_at"] if roster else None
    if version is None:
        check_version = roster_version
        if not check_version:
            # 名册环境列尚未切（如注入『跳过名册』），但切流事件已出：
            # 仍需按事件目标版本核对探针，才能把「名册未切」显形而不是退回未切流。
            intent = _latest_forward_event(conn, app_id, env_id)
            if intent:
                check_version = intent["target"] or ""
    else:
        check_version = version or ""

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
        "切流证据": {},
        "切流状态": CUTOVER_NONE,
        "切流成功": False,
        "证据缺口标": "",
        "名册已切": False,
        "探针过线": False,
        "切流事件": False,
        "审计切流": False,
        "缺证": [],
    }

    if not check_version:
        # 既无名册版本也无切流事件：纯基线/未切流，证据无需可算。
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
        inst["名册版本一致"] = True

    # 切流成功灯必须四件同核；缺口/证据缺项在此一并挂到槽位上，避免
    # 「回执亮成功而探针红/名册未切」撕裂。
    evidence = cutover_evidence(
        conn, app_id, env_id, version=(check_version or None), threshold=threshold, slot=slot
    )
    slot["切流证据"] = evidence
    slot["切流状态"] = evidence["切流状态"]
    slot["切流成功"] = evidence["切流成功"]
    slot["证据缺口标"] = evidence["证据缺口标"]
    slot["名册已切"] = evidence["名册已切"]
    slot["探针过线"] = evidence["探针过线"]
    slot["切流事件"] = evidence["切流事件"]
    slot["审计切流"] = evidence["审计切流"]
    slot["缺证"] = evidence["缺证"]
    if evidence["证据缺口标"]:
        slot["是否缺口"] = True
        if not slot["缺口标"]:
            slot["缺口标"] = evidence["证据缺口标"]
    for inst in instances:
        inst["切流状态"] = slot["切流状态"]
        inst["切流成功"] = slot["切流成功"]
        inst["证据缺口标"] = slot["证据缺口标"]
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
    # 发版详情环境列与名册当前列同一口径：切流状态/证据直接来自唯一评估核心。
    release["切流状态"] = slot["切流状态"]
    release["切流成功"] = slot["切流成功"]
    release["证据缺口标"] = slot["证据缺口标"]
    release["缺证"] = slot["缺证"]
    release["名册版本"] = slot["名册版本"]
    release["名册已切"] = slot["名册已切"]
    release["放行结局"] = {
        k: slot[k]
        for k in (
            "结局", "缺口标", "是否缺口", "红灯已切流", "名册一致",
            "名册版本", "核对版本", "阈值", "探针失败次数阈值", "失败次数阈值",
            "实例总数", "匹配实例数",
            "绿数", "红灯数", "待报数", "超标数", "失败次数",
            "切流状态", "切流成功", "证据缺口标", "缺证", "切流证据",
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
                  created: bool = False, state: str | None = None) -> int:
    now = now_str()
    st = state if state is not None else slot.get("切流状态", CUTOVER_NONE)
    if created:
        cur = conn.execute(
            """
            INSERT INTO switch_ledger
              (switch_key, release_id, app_id, env_id, version,
               roster_committed, attached_count, outcome, gap_mark, state,
               created_at, updated_at, replayed_count)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(switch_key) DO UPDATE SET
               attached_count=excluded.attached_count,
               outcome=excluded.outcome,
               gap_mark=excluded.gap_mark,
               state=excluded.state,
               updated_at=excluded.updated_at
            """,
            (switch_key, release_id, app_id, env_id, version,
             attached_count, slot["结局"], slot["缺口标"], st, now, now),
        )
        row = conn.execute(
            "SELECT id FROM switch_ledger WHERE switch_key=?", (switch_key,)
        ).fetchone()
        return row["id"]
    conn.execute(
        """
        UPDATE switch_ledger
        SET attached_count=?, outcome=?, gap_mark=?, state=?, updated_at=?
        WHERE switch_key=?
        """,
        (attached_count, slot["结局"], slot["缺口标"], st, now, switch_key),
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
            SET outcome=?, gap_mark=?, attached_count=?, state=?, updated_at=?
            WHERE switch_key=?
            """,
            (slot["结局"], slot["缺口标"], attached,
             slot.get("切流状态", CUTOVER_NONE), now, r["switch_key"]),
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
                SET outcome=?, gap_mark=?, attached_count=?, state=?, updated_at=?,
                    replayed_count=replayed_count+1
                WHERE id=?
                """,
                (slot["结局"], slot["缺口标"], slot["匹配实例数"],
                 slot.get("切流状态", CUTOVER_NONE), now, row["id"]),
            )
        else:
            conn.execute(
                """
                UPDATE switch_ledger
                SET outcome=?, gap_mark=?, attached_count=?, state=?, updated_at=?
                WHERE id=?
                """,
                (slot["结局"], slot["缺口标"], slot["匹配实例数"],
                 slot.get("切流状态", CUTOVER_NONE), now, row["id"]),
            )
        replayed.append(
            {"台账编号": row["id"], "switch_key": row["switch_key"],
             "结局": slot["结局"], "缺口标": slot["缺口标"],
             "切流状态": slot.get("切流状态", CUTOVER_NONE),
             "切流成功": bool(slot.get("切流成功"))}
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


# ---------------------------------------------------------------------------
# 切流差额 / 审计时序：让「回执出而探针红」「审计有健康未过」「切流桶与审计分叉」
# 都能被同一条纯计算线揭穿。
# ---------------------------------------------------------------------------

def audit_timeline(conn, threshold: int | None = None) -> list[dict]:
    """每条切流审计记录的『当时结局』与『当前结局』并读。

    审计里存的是一次快照；切流灯只能决定在当下是否还站得住。若当时记
    探针全绿、现在探针红/名册未切/证据缺项，这就是一条『审计分叉』。
    """
    if threshold is None:
        threshold = fail_threshold(conn)
    rows = conn.execute(
        """
        SELECT id AS 编号, ref_id AS 关联编号, action AS 动作,
               created_at AS 时间, note AS 备注
        FROM audit_events WHERE kind='切流'
        ORDER BY id ASC
        """
    ).fetchall()
    timeline: list[dict] = []
    for r in rows:
        rid = r["关联编号"]
        note = r["备注"] or ""
        recorded = ""
        for token in note.split():
            if token.startswith("结局="):
                recorded = token.split("=", 1)[1]
        release = conn.execute(
            "SELECT app_id, current_env_id, target_version FROM releases WHERE id=?", (rid,)
        ).fetchone()
        current = ""
        state = ""
        diverged = False
        if release and release["app_id"] and release["current_env_id"]:
            # 审计对应的槽位以切流事件的 to_env 为准；回滚后 current_env_id
            # 仍可能停留在被回滚环境，这里不依赖它。
            ev = conn.execute(
                """
                SELECT pe.to_env_id AS env_id FROM promote_events pe
                WHERE pe.release_id=? AND pe.decision='放行'
                ORDER BY pe.id DESC LIMIT 1
                """,
                (rid,),
            ).fetchone()
            env_id = ev["env_id"] if ev else release["current_env_id"]
            # 审计当时核对的是切流那一刻的版本；必须按该段自己的版本重放。
            slot = evaluate_slot(
                conn, release["app_id"], env_id,
                version=release["target_version"], threshold=threshold,
            )
            current = slot["结局"]
            state = slot["切流状态"]
            ev_rec = slot.get("切流证据") or {}
            # 分叉正是「审计当时与当下重放不一致」：
            # 当时记绿，当下探针红/名册未切/证据缺/成功灯不亮 → 假切流回执被揭穿。
            if recorded == GREEN:
                diverged = (
                    current != GREEN
                    or bool(ev_rec.get("证据缺口标"))
                    or bool(ev_rec.get("缺证"))
                    or not slot.get("切流成功")
                )
            elif recorded and recorded != current:
                diverged = True
        timeline.append({
            "编号": r["编号"],
            "关联编号": rid,
            "动作": r["动作"],
            "时间": r["时间"],
            "审计当时结局": recorded,
            "当前结局": current,
            "当前切流状态": state,
            "分叉": diverged,
            "备注": note,
        })
    return timeline


def cutover_delta(conn, threshold: int | None = None) -> list[dict]:
    """切流差额：名册当前列 vs 最新切流事件的逐槽差额。

    差额行是切流桶「不计已切」的直接证据：名册未切、探针未过、证据缺、
    已回滚收束都会在此显形，不靠前端自行推断。
    """
    if threshold is None:
        threshold = fail_threshold(conn)
    rows = conn.execute(
        """
        SELECT v.app_id AS 应用编号, v.env_id AS 环境编号
        FROM app_env_versions v ORDER BY v.app_id, v.env_id
        """
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        slot = evaluate_slot(conn, r["应用编号"], r["环境编号"], threshold=threshold)
        ev = slot.get("切流证据") or {}
        if not ev.get("适用"):
            continue
        intent_target = (ev.get("切流目标版本") or "")
        compare_target = (ev.get("切流核对版本") or intent_target)
        roster_version = slot.get("名册版本") or ""
        state = slot.get("切流状态", CUTOVER_NONE)
        delta = ""
        if compare_target and roster_version != compare_target:
            # 回滚半截：名册尚未切回 —— 与正常「名册未切」分开标注。
            delta = "回滚半截" if state == CUTOVER_ROLLED_BACK else "名册未切"
        elif state == CUTOVER_ROLLED_BACK:
            # 回滚已收束：前向切流已收束，仍需单列在案，但不作为长期噪声差额。
            continue
        elif state == CUTOVER_FROZEN:
            delta = "探针未过" if not ev.get("探针过线") else "证据缺"
        if not delta:
            continue
        out.append({
            "应用编号": r["应用编号"],
            "环境编号": r["环境编号"],
            "应用": slot["应用"],
            "环境": slot["环境"],
            "名册版本": roster_version,
            "切流目标版本": intent_target,
            "切流状态": state,
            "差额": delta,
            "结局": slot["结局"],
            "缺口标": slot["缺口标"] if slot["是否缺口"] else ev.get("证据缺口标", ""),
            "缺证": ev.get("缺证", []),
            "探针过线": bool(ev.get("探针过线")),
            "名册已切": bool(ev.get("名册已切")),
            "切流发版单编号": ev.get("切流发版单编号"),
        })
    return out


def roster_vs_release_env(conn, threshold: int | None = None) -> list[dict]:
    """发版详情环境列 vs 名册当前列：不得长期双账。

    对最近一条生产放行单比较 release.current_env_id/目标版本与名册当前列；
    不一致即「双账」，逐行可核（不是只改一个提示词）。
    """
    if threshold is None:
        threshold = fail_threshold(conn)
    prod = conn.execute(
        "SELECT id FROM environments WHERE code=?", (PROD_ENV_CODE,)
    ).fetchone()
    if not prod:
        return []
    rows = conn.execute(
        """
        SELECT r.id AS 发版单编号, r.serial AS 发版单号, r.app_id AS 应用编号,
               r.status AS 状态, r.target_version AS 目标版本,
               r.rollback_version AS 回滚版本, a.name AS 应用
        FROM releases r JOIN apps a ON a.id=r.app_id
        WHERE r.current_env_id=? AND r.status IN ('已放行','晋升中','已回滚','回滚中')
        ORDER BY r.id DESC
        """,
        (prod["id"],),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        slot = evaluate_slot(conn, r["应用编号"], prod["id"], threshold=threshold)
        roster_version = slot.get("名册版本") or ""
        # 已回滚/回滚中的单，详情里的当前版本应是回滚版本；否则才用目标版本。
        if r["状态"] in ("已回滚", "回滚中") and r["回滚版本"]:
            detail_version = r["回滚版本"]
        else:
            detail_version = r["目标版本"] or ""
        double = bool(roster_version) and roster_version != detail_version
        out.append({
            "发版单编号": r["发版单编号"],
            "发版单号": r["发版单号"],
            "应用": r["应用"],
            "状态": r["状态"],
            "发版详情版本": detail_version,
            "发版详情环境": PROD_ENV_CODE,
            "名册当前版本": roster_version,
            "名册当前环境": PROD_ENV_CODE,
            "回滚版本": r["回滚版本"] or "",
            "双账": double,
            "切流状态": slot.get("切流状态", CUTOVER_NONE),
            "切流成功": bool(slot.get("切流成功")),
        })
    return out

