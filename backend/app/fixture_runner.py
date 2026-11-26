"""可复现夹具套件：放行 ↔ 探针关系统一结局。

四套夹具（前两套必须分开复现）：
  1. red_after_switch   切流后打红：绿放行 → 连续打红 → 计数 → 阈值超标
  2. red_while_released 红灯仍放行：放行前探针已红且超标，放行不得抹平，整笔仍已放行但缺口全站可见
  3. half_attach        半截无实例挂接：注入“跳过挂接”，先核缺口，再幂等重放补挂，再转绿
  4. concurrent_flip    切流与探针翻转并发：栅栏同时开打，原子计数保证结局可复现、可重放

本模块直接调用各路由处理函数（同一套写口/读口逻辑与事务），
CLI 与 /api/夹具 端点都复用它。
"""
from __future__ import annotations

import threading
import traceback
import uuid
from datetime import datetime, timedelta

from app.db import db
from app.seed import seed_if_empty
from app.routers import apps as apps_router
from app.routers import audit as audit_router
from app.routers import dashboard as dashboard_router
from app.routers import health as health_router
from app.routers import promote as promote_router
from app.routers import reconcile as reconcile_router
from app.routers import releases as releases_router
from app.routers.apps import AppIn
from app.routers.health import HealthIn, InstanceIn
from app.routers.promote import PromoteIn
from app.routers.releases import PipelineIn, ReleaseIn
from app.services.common import now_str
from app.services.outcomes import (
    GAP_NO_INSTANCE,
    GAP_PENDING,
    GAP_RED_OVER,
    GREEN,
    NO_INSTANCE,
    PENDING,
    POLICY_LABEL,
    RED_OVER,
)

PROD = "产"


def _check(checks: list, name: str, ok: bool, expected="", actual=""):
    checks.append({"检查项": name, "通过": bool(ok), "期望": expected, "实际": actual})


def ensure_fixture_basics(threshold: int = 3) -> int:
    """夹具自包含：建表/迁移/种子、固定阈值、保证三环境放行窗开着。"""
    seed_if_empty()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES('探针失败阈值', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(threshold),),
        )
        env_ids = [r["id"] for r in conn.execute("SELECT id FROM environments").fetchall()]
        open_at = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        close_at = (datetime.now() + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
        for eid in env_ids:
            open_win = conn.execute(
                "SELECT id FROM release_windows WHERE env_id=? AND status='窗开'", (eid,)
            ).fetchone()
            if not open_win:
                conn.execute(
                    "INSERT INTO release_windows(env_id, open_at, close_at, status) VALUES (?, ?, ?, '窗开')",
                    (eid, open_at, close_at),
                )
    return threshold


def _new_train(tag: str, version: str = "2.4.0"):
    suffix = uuid.uuid4().hex[:8]
    app = apps_router.create_app(AppIn(名称=f"夹具-{tag}-{suffix}", 负责人="夹具", 状态="启用"))
    app_id = app["编号"]
    rel = releases_router.create_release(
        ReleaseIn(应用编号=app_id, 目标版本=version, 制品摘要="sha256:fixture")
    )
    rid = rel["编号"]
    releases_router.set_pipeline(rid, PipelineIn(流水线="绿"))
    releases_router.admit(rid)  # 入闸即落地 测
    promote_router.promote(rid, PromoteIn(目标环境="预", 备注="夹具入预"))
    return app_id, rid, version


def _find(items: list, app_id: int, env: str | None = PROD):
    for x in items:
        if x.get("应用编号") == app_id and (env is None or x.get("环境") == env):
            return x
    return None


def collect_alignment(app_id: int, rid: int, version: str,
                      expected_outcome: str, expected_gap: str,
                      switch_audit_outcome: str | None = None) -> tuple[dict, list]:
    """把放行回执之外的所有定位面取齐，并校验它们共享同一结局。"""
    checks: list = []

    release = releases_router.one_release(rid)
    roster = apps_router.list_roster()
    roster_row = _find(roster, app_id, PROD)
    instances = [
        x for x in health_router.list_instances()
        if x.get("应用编号") == app_id and x.get("环境") == PROD
    ]
    overview = dashboard_router.overview()
    ov_version = _find(overview["环境版本"], app_id, PROD)
    ov_red = _find(overview["红灯已切流"], app_id, PROD)
    ov_gap = _find(overview["缺口台账"], app_id, PROD)
    reconcile = reconcile_router.reconcile()
    rec_row = _find(reconcile["名册与实例"], app_id, PROD)
    rec_gap = _find(reconcile["缺口台账"], app_id, PROD)
    audits = audit_router.list_audit(类别="切流", 关联编号=rid)
    switch_audit = next((a for a in audits if a["动作"] == "切流登记"), None)

    _check(checks, "发版单为已放行", release.get("状态") == "已放行", "已放行", release.get("状态"))
    _check(checks, "发版单结局一致", release.get("结局") == expected_outcome,
           expected_outcome, release.get("结局"))

    _check(checks, "名册产版本已切到目标版本",
           roster_row and roster_row.get("名册版本") == version,
           version, roster_row and roster_row.get("名册版本"))
    _check(checks, "名册结局一致",
           roster_row and roster_row.get("结局") == expected_outcome,
           expected_outcome, roster_row and roster_row.get("结局"))
    _check(checks, "名册缺口标一致",
           roster_row and roster_row.get("缺口标") == expected_gap,
           expected_gap, roster_row and roster_row.get("缺口标"))

    _check(checks, "总览环境版本结局一致",
           ov_version and ov_version.get("结局") == expected_outcome,
           expected_outcome, ov_version and ov_version.get("结局"))
    _check(checks, "对账行结局一致",
           rec_row and rec_row.get("结局") == expected_outcome,
           expected_outcome, rec_row and rec_row.get("结局"))
    _check(checks, "对账缺口标一致",
           rec_row and rec_row.get("缺口标") == expected_gap,
           expected_gap, rec_row and rec_row.get("缺口标"))

    if expected_gap:
        _check(checks, "对账缺口台账挂账", rec_gap is not None and rec_gap.get("缺口标") == expected_gap,
               expected_gap, rec_gap and rec_gap.get("缺口标"))
        _check(checks, "总览缺口台账挂账", ov_gap is not None and ov_gap.get("缺口标") == expected_gap,
               expected_gap, ov_gap and ov_gap.get("缺口标"))
    else:
        _check(checks, "无缺口时对账缺口台账不挂账", rec_gap is None, None, "存在" if rec_gap else None)

    if expected_outcome == RED_OVER or expected_outcome == "红灯计数中":
        _check(checks, "总览红灯已切流露口",
               ov_red is not None and ov_red.get("红灯已切流") is True,
               True, ov_red and ov_red.get("红灯已切流"))
        _check(checks, "对账红灯已切流标记",
               rec_row and rec_row.get("红灯已切流") is True,
               True, rec_row and rec_row.get("红灯已切流"))
    else:
        _check(checks, "非红灯结局不进总览红灯已切流", ov_red is None, None, "存在" if ov_red else None)

    _check(checks, "审计切流记录存在", switch_audit is not None, "切流登记",
           switch_audit and switch_audit.get("动作"))
    if switch_audit_outcome and switch_audit:
        _check(checks, "审计切流记录结局与当时回执一致",
               f"结局={switch_audit_outcome}" in (switch_audit.get("备注") or ""),
               f"结局={switch_audit_outcome}", switch_audit.get("备注"))

    bundle = {
        "发版单": release,
        "名册产版本": roster_row,
        "实例健康行": instances,
        "总览环境版本行": ov_version,
        "总览红灯已切流露口": ov_red,
        "总览缺口行": ov_gap,
        "对账行": rec_row,
        "对账缺口标": rec_gap,
        "审计切流记录": switch_audit,
    }
    return bundle, checks


def scenario_red_after_switch() -> dict:
    threshold = ensure_fixture_basics()
    steps: list = []
    checks: list = []
    app_id, rid, version = _new_train("切流后打红")

    result = promote_router.promote(rid, PromoteIn(目标环境=PROD, 备注="夹具绿放行"))
    receipt = result["放行回执"]
    _check(checks, "绿放行回执结局", receipt["结局"] == GREEN, GREEN, receipt["结局"])
    iid = receipt["实例编号"]
    steps.append({"步骤": "放行", "回执结局": receipt["结局"], "实例编号": iid})

    first = health_router.set_health(iid, HealthIn(健康="探针红"))
    _check(checks, "首次打红失败次数=1", first["失败次数"] == 1, 1, first["失败次数"])
    _check(checks, "未达阈值为红灯计数中", first["结局"] == "红灯计数中", "红灯计数中", first["结局"])
    steps.append({"步骤": "打红1次", "失败次数": first["失败次数"], "结局": first["结局"]})

    health_router.set_health(iid, HealthIn(健康="探针红"))
    crossing = health_router.set_health(iid, HealthIn(健康="探针红"))
    _check(checks, f"连红{threshold}次到达阈值", crossing["失败次数"] == threshold,
           threshold, crossing["失败次数"])
    _check(checks, "阈值点升级红灯超标", crossing["结局"] == RED_OVER, RED_OVER, crossing["结局"])
    steps.append({"步骤": f"打红{threshold}次(阈值点)", "失败次数": crossing["失败次数"], "结局": crossing["结局"]})

    final = health_router.set_health(iid, HealthIn(健康="探针红"))
    _check(checks, "原子累计到阈值+1", final["失败次数"] == threshold + 1,
           threshold + 1, final["失败次数"])
    steps.append({"步骤": "再打红1次", "失败次数": final["失败次数"], "结局": final["结局"]})

    bundle, align_checks = collect_alignment(
        app_id, rid, version, RED_OVER, GAP_RED_OVER, switch_audit_outcome=GREEN
    )
    checks.extend(align_checks)
    probe_audits = audit_router.list_audit(类别="探针", 关联编号=iid)
    _check(checks, "探针审计含连续失败次数",
           any(f"连续失败 {threshold + 1}/{threshold}" in (a.get("备注") or "") for a in probe_audits),
           f"连续失败 {threshold + 1}/{threshold}",
           [a.get("备注") for a in probe_audits[:1]])
    bundle["探针审计"] = probe_audits
    bundle["放行回执"] = receipt
    bundle["探针失败次数"] = {"阈值": threshold, "最终失败次数": final["失败次数"]}
    return _result("切流后打红", "red_after_switch", threshold, steps, checks, bundle)


def scenario_red_while_released() -> dict:
    threshold = ensure_fixture_basics()
    steps: list = []
    app_id, rid, version = _new_train("红灯仍放行")

    # 放行写口调用之前，名册产版本还是旧值，但目标版本实例已经红透。
    registered = health_router.register_instance(
        InstanceIn(应用编号=app_id, 环境=PROD, 版本=version,
                   健康="探针红", 失败次数=threshold + 2)
    )
    _check_pre: list = []
    checks: list = []
    _check(checks, "放行前红实例已登记", registered["健康"] == "探针红", "探针红", registered["健康"])
    steps.append({"步骤": "放行前登记红灯实例", "失败次数": registered["失败次数"]})

    result = promote_router.promote(rid, PromoteIn(目标环境=PROD, 备注="夹具红灯仍放行"))
    receipt = result["放行回执"]
    _check(checks, "红灯不阻塞放行写口", receipt["状态"] == "已放行", "已放行", receipt["状态"])
    _check(checks, "放行不抹平既有失败次数",
           receipt["失败次数"] == threshold + 2, threshold + 2, receipt["失败次数"])
    _check(checks, "回执结局为红灯超标", receipt["结局"] == RED_OVER, RED_OVER, receipt["结局"])
    steps.append({"步骤": "红灯状态下放行", "失败次数": receipt["失败次数"], "结局": receipt["结局"]})

    iid = receipt["实例编号"] or registered["编号"]
    moved = health_router.set_health(iid, HealthIn(健康="探针红"))
    _check(checks, "失败次数在保留值上继续原子累加",
           moved["失败次数"] == threshold + 3, threshold + 3, moved["失败次数"])

    bundle, align_checks = collect_alignment(
        app_id, rid, version, RED_OVER, GAP_RED_OVER, switch_audit_outcome=RED_OVER
    )
    checks.extend(align_checks)
    bundle["放行回执"] = receipt
    bundle["探针失败次数"] = {"阈值": threshold, "最终失败次数": moved["失败次数"]}
    return _result("红灯仍放行", "red_while_released", threshold, steps, checks, bundle)


def scenario_half_attach() -> dict:
    threshold = ensure_fixture_basics()
    steps: list = []
    checks: list = []
    app_id, rid, version = _new_train("半截挂接")

    result = promote_router.promote(
        rid, PromoteIn(目标环境=PROD, 故障注入="跳过挂接", 备注="夹具半截")
    )
    receipt = result["放行回执"]
    _check(checks, "半截回执判为无实例挂接",
           receipt["结局"] == NO_INSTANCE, NO_INSTANCE, receipt["结局"])
    _check(checks, "半截回执无实例编号", receipt["实例编号"] is None, None, receipt["实例编号"])
    steps.append({"步骤": "注入跳过挂接", "结局": receipt["结局"]})

    before, align_before = collect_alignment(
        app_id, rid, version, NO_INSTANCE, GAP_NO_INSTANCE,
        switch_audit_outcome=NO_INSTANCE,
    )
    checks.extend(align_before)
    _check(checks, "半截态实例健康行为空", before["实例健康行"] == [], 0, len(before["实例健康行"]))
    steps.append({"步骤": "半截态各面核对", "对账缺口标": before["对账缺口标"] and before["对账缺口标"]["缺口标"]})

    replay1 = reconcile_router.replay()
    repaired_here = [x for x in replay1["补挂接"] if x.get("应用编号") == app_id]
    _check(checks, "重放幂等补挂本应用实例", len(repaired_here) == 1, 1, len(repaired_here))
    new_iid = repaired_here[0]["实例编号"] if repaired_here else None
    steps.append({"步骤": "第一次重放补挂", "新实例编号": new_iid})

    after_pending, align_pending = collect_alignment(
        app_id, rid, version, PENDING, GAP_PENDING
    )
    checks.extend(align_pending)
    _check(checks, "补挂行是探针待报而非误亮",
           after_pending["实例健康行"]
           and after_pending["实例健康行"][0]["健康"] == "探针待报",
           "探针待报",
           after_pending["实例健康行"] and after_pending["实例健康行"][0]["健康"])

    replay2 = reconcile_router.replay()
    repaired_again = [x for x in replay2["补挂接"] if x.get("应用编号") == app_id]
    _check(checks, "第二次重放不重复补挂", repaired_again == [], [], len(repaired_again))
    steps.append({"步骤": "第二次重放(幂等)", "补挂接数": replay2["补挂接数"]})

    green = health_router.set_health(new_iid, HealthIn(健康="探针绿"))
    _check(checks, "补挂实例可探并转绿", green["结局"] == GREEN, GREEN, green["结局"])
    after_green, align_green = collect_alignment(app_id, rid, version, GREEN, "")
    checks.extend(align_green)
    steps.append({"步骤": "探针回报绿", "结局": green["结局"]})

    bundle = {
        "放行回执": receipt,
        "半截态": before,
        "重放报告首次": replay1,
        "待首探态": after_pending,
        "重放报告二次": replay2,
        "转绿后": after_green,
        "名册产版本": after_green["名册产版本"],
        "实例健康行": after_green["实例健康行"],
        "探针失败次数": {"阈值": threshold, "最终失败次数": 0},
        "对账缺口标": before["对账缺口标"],
        "审计切流记录": after_green["审计切流记录"],
        "总览红灯已切流露口": after_green["总览红灯已切流露口"],
    }
    return _result("半截无实例挂接", "half_attach", threshold, steps, checks, bundle)


def scenario_concurrent_flip(rounds: int = 1, red_workers: int | None = None) -> dict:
    threshold = ensure_fixture_basics()
    n_red = red_workers or (threshold + 2)
    round_reports: list = []
    checks: list = []

    for round_no in range(1, max(1, rounds) + 1):
        app_id, rid, version = _new_train(f"并发翻转r{round_no}")
        reg = health_router.register_instance(
            InstanceIn(应用编号=app_id, 环境=PROD, 版本=version, 健康="探针绿")
        )
        iid = reg["编号"]

        barrier = threading.Barrier(n_red + 1)
        errors: list[str] = []
        promote_holder: dict = {}

        def red_worker():
            try:
                barrier.wait(timeout=30)
                for _ in range(1):
                    health_router.set_health(iid, HealthIn(健康="探针红"))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"red:{exc!r}")

        def promote_worker():
            try:
                barrier.wait(timeout=30)
                promote_holder["result"] = promote_router.promote(
                    rid, PromoteIn(目标环境=PROD, 备注="夹具并发切流")
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"promote:{exc!r}")

        threads = [threading.Thread(target=red_worker) for _ in range(n_red)]
        t_promote = threading.Thread(target=promote_worker)
        for t in threads:
            t.start()
        t_promote.start()
        for t in threads:
            t.join(timeout=60)
        t_promote.join(timeout=60)

        _check(checks, f"[r{round_no}] 并发线程无错误", not errors, [], errors)
        final_inst = next(
            (x for x in health_router.list_instances() if x["编号"] == iid), None
        )
        _check(checks, f"[r{round_no}] 失败次数原子累计不丢失",
               final_inst and final_inst["失败次数"] == n_red,
               n_red, final_inst and final_inst["失败次数"])
        _check(checks, f"[r{round_no}] 最终探针红",
               final_inst and final_inst["健康"] == "探针红",
               "探针红", final_inst and final_inst["健康"])

        release = releases_router.one_release(rid)
        _check(checks, f"[r{round_no}] 发版单已放行",
               release["状态"] == "已放行", "已放行", release["状态"])

        bundle, align_checks = collect_alignment(
            app_id, rid, version, RED_OVER, GAP_RED_OVER
        )
        checks.extend([
            {**c, "检查项": f"[r{round_no}] {c['检查项']}"} for c in align_checks
        ])

        snap1 = {(x["应用编号"], x["环境"]): (x["结局"], x["缺口标"])
                 for x in reconcile_router.reconcile()["名册与实例"]}
        reconcile_router.replay()
        snap2 = {(x["应用编号"], x["环境"]): (x["结局"], x["缺口标"])
                 for x in reconcile_router.reconcile()["名册与实例"]}
        reconcile_router.replay()
        snap3 = {(x["应用编号"], x["环境"]): (x["结局"], x["缺口标"])
                 for x in reconcile_router.reconcile()["名册与实例"]}
        _check(checks, f"[r{round_no}] 重放结局稳定可复现",
               snap1 == snap2 == snap3, "三次快照一致", "不一致")
        round_reports.append({
            "轮次": round_no,
            "红灯并发数": n_red,
            "错误": errors,
            "最终失败次数": final_inst and final_inst["失败次数"],
            "产物": bundle,
        })

    bundle = {"轮次报告": round_reports}
    if round_reports:
        last = round_reports[-1]
        bundle.update(last["产物"])
        # 与其余三套夹具保持同一组七件对齐产物。
        last_receipt = (promote_holder.get("result") or {}).get("放行回执")
        if last_receipt is not None:
            bundle["放行回执"] = last_receipt
        bundle["探针失败次数"] = {
            "阈值": threshold,
            "最终失败次数": last.get("最终失败次数"),
            "并发红数": n_red,
        }
    return _result("切流与探针并发翻转", "concurrent_flip", threshold, [], checks, bundle)


def _result(title: str, key: str, threshold: int, steps: list, checks: list,
            artifacts: dict) -> dict:
    passed = all(c["通过"] for c in checks)
    return {
        "夹具": title,
        "标识": key,
        "通过": passed,
        "策略": POLICY_LABEL,
        "阈值": threshold,
        "时间": now_str(),
        "步骤": steps,
        "断言": checks,
        "断言通过数": sum(1 for c in checks if c["通过"]),
        "断言总数": len(checks),
        "产物": artifacts,
    }


def run_suite(concurrent_rounds: int = 1) -> dict:
    cases = [
        _safe("切流后打红", "red_after_switch", scenario_red_after_switch),
        _safe("红灯仍放行", "red_while_released", scenario_red_while_released),
        _safe("半截无实例挂接", "half_attach", scenario_half_attach),
        _safe("并发翻转", "concurrent_flip",
              lambda: scenario_concurrent_flip(rounds=concurrent_rounds)),
    ]
    all_pass = all(c.get("通过") for c in cases)
    return {
        "夹具": "放行-探针统一结局全套",
        "全部通过": all_pass,
        "策略": POLICY_LABEL,
        "时间": now_str(),
        "用例数": len(cases),
        "通过数": sum(1 for c in cases if c.get("通过")),
        "用例": cases,
    }


def _safe(title: str, key: str, fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {
            "夹具": title,
            "标识": key,
            "通过": False,
            "异常": repr(exc),
            "堆栈": traceback.format_exc(),
            "断言": [],
            "产物": {},
        }
