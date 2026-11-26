from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list
from app.services.common import RELEASE_SELECT, audit
from app.services.outcomes import (
    POLICY_LABEL,
    audit_timeline,
    cutover_delta,
    enrich_release,
    evaluate_slot,
    fail_threshold,
    replay_outcomes,
    roster_views,
    roster_vs_release_env,
)
router = APIRouter(tags=["对账"])


@router.get("/对账")
def reconcile():
    with db() as conn:
        threshold = fail_threshold(conn)
        release_stats = {
            "申请数": conn.execute("SELECT COUNT(*) AS c FROM releases").fetchone()["c"],
            "已放行": conn.execute(
                "SELECT COUNT(*) AS c FROM releases WHERE status='已放行'"
            ).fetchone()["c"],
            "已回滚": conn.execute(
                "SELECT COUNT(*) AS c FROM releases WHERE status='已回滚'"
            ).fetchone()["c"],
            "在途": conn.execute(
                """
                SELECT COUNT(*) AS c FROM releases
                WHERE status IN ('已入闸','晋升中','已放行','回滚中')
                """
            ).fetchone()["c"],
        }
        version_vs_instance = rows_to_list(
            conn.execute(
                """
                SELECT v.app_id AS 应用编号, v.env_id AS 环境编号,
                       a.name AS 应用, e.code AS 环境,
                       v.version AS 名册版本,
                       GROUP_CONCAT(i.version, ',') AS 实例版本们,
                       SUM(CASE WHEN i.health='探针红' THEN 1 ELSE 0 END) AS 环境红灯总数,
                       SUM(CASE WHEN i.health='探针红' THEN i.fail_count ELSE 0 END) AS 环境失败次数和
                FROM app_env_versions v
                JOIN apps a ON a.id = v.app_id
                JOIN environments e ON e.id = v.env_id
                LEFT JOIN instances i ON i.app_id = v.app_id AND i.env_id = v.env_id
                GROUP BY v.app_id, v.env_id
                ORDER BY a.id, e.sort_order
                """
            ).fetchall()
        )

        # 用统一结局核心覆盖每一行的成功标准，避免“对账记已放行而实例全红”。
        slots = {
            (s["应用编号"], s["环境编号"]): s for s in roster_views(conn, threshold)
        }
        for row in version_vs_instance:
            slot = slots.get((row["应用编号"], row["环境编号"]))
            if slot:
                row["结局"] = slot["结局"]
                row["缺口标"] = slot["缺口标"]
                row["是否缺口"] = slot["是否缺口"]
                row["红灯已切流"] = slot["红灯已切流"]
                row["阈值"] = threshold
                row["探针失败次数阈值"] = threshold
                row["失败次数阈值"] = threshold
                row["匹配实例数"] = slot["匹配实例数"]
                row["绿数"] = slot["绿数"]
                row["红灯数"] = slot["红灯数"]
                row["待报数"] = slot["待报数"]
                row["超标数"] = slot["超标数"]
                row["失败次数"] = slot["失败次数"]
                row["切流状态"] = slot.get("切流状态", "")
                row["切流成功"] = bool(slot.get("切流成功"))
                row["证据缺口标"] = slot.get("证据缺口标", "")
                row["缺证"] = slot.get("缺证", [])
                row["对齐"] = (
                    slot["结局"] in ("探针全绿", "未切流")
                    and not slot.get("证据缺口标")
                )
            else:
                row["结局"] = "未切流"
                row["缺口标"] = ""
                row["阈值"] = threshold
                row["探针失败次数阈值"] = threshold
                row["对齐"] = True

        gap_ledger = [
            {
                "应用编号": s["应用编号"],
                "环境编号": s["环境编号"],
                "应用": s["应用"],
                "环境": s["环境"],
                "名册版本": s["名册版本"],
                "结局": s["结局"],
                "缺口标": s["缺口标"],
                "红灯已切流": s["红灯已切流"],
                "匹配实例数": s["匹配实例数"],
                "红灯数": s["红灯数"],
                "失败次数": s["失败次数"],
                "阈值": s["阈值"],
                "探针失败次数阈值": s["探针失败次数阈值"],
                "切流状态": s.get("切流状态", ""),
                "证据缺口标": s.get("证据缺口标", ""),
                "缺证": s.get("缺证", []),
            }
            for s in slots.values()
            if s["是否缺口"]
        ]
        red_switched = [g for g in gap_ledger if g["红灯已切流"]]

        promote_count = conn.execute("SELECT COUNT(*) AS c FROM promote_events").fetchone()["c"]
        switch_count = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_events WHERE kind='切流'"
        ).fetchone()["c"]
        switch_audits = rows_to_list(
            conn.execute(
                """
                SELECT id AS 编号, kind AS 类别, ref_id AS 关联编号,
                       action AS 动作, created_at AS 时间, note AS 备注
                FROM audit_events WHERE kind='切流'
                ORDER BY id DESC LIMIT 100
                """
            ).fetchall()
        )
        ledger_count = conn.execute("SELECT COUNT(*) AS c FROM switch_ledger").fetchone()["c"]
        active = rows_to_list(
            conn.execute(
                RELEASE_SELECT
                + " WHERE r.status IN ('已入闸','晋升中','已放行','回滚中') ORDER BY r.id DESC"
            ).fetchall()
        )
        active = [enrich_release(conn, r, threshold) for r in active]

        # 切流事件重放：对账按台账逐笔重放四件证据，揭穿「回执出而探针红」
        # 「名册切了健康未过」的假成功。
        ledger_rows = rows_to_list(
            conn.execute(
                """
                SELECT l.id AS 台账编号, l.switch_key AS switch_key,
                       l.release_id AS 发版单编号, l.app_id AS 应用编号,
                       l.env_id AS 环境编号, r.serial AS 发版单号,
                       a.name AS 应用, e.code AS 环境, l.version AS 版本,
                       l.outcome AS 台账结局, l.gap_mark AS 台账缺口标,
                       l.state AS 台账状态
                FROM switch_ledger l
                JOIN releases r ON r.id=l.release_id
                JOIN apps a ON a.id=l.app_id
                JOIN environments e ON e.id=l.env_id
                ORDER BY l.id
                """
            ).fetchall()
        )
        switch_event_replay = []
        for lrow in ledger_rows:
            # 台账自带 app_id/env_id/version：按台账三件套重放，
            # 不能用发版单当前环境（回滚后可能已变），否则重放会错槽。
            slot = evaluate_slot(
                conn, lrow["应用编号"], lrow["环境编号"], version=lrow["版本"],
                threshold=threshold,
            )
            ev = slot.get("切流证据") or {}
            claims_success = lrow["台账结局"] == "探针全绿" and bool(ev.get("适用"))
            now_success = bool(slot.get("切流成功"))
            switch_event_replay.append({
                "台账编号": lrow["台账编号"],
                "发版单编号": lrow["发版单编号"],
                "发版单号": lrow["发版单号"],
                "应用": lrow["应用"],
                "环境": lrow["环境"],
                "版本": lrow["版本"],
                "台账当时结局": lrow["台账结局"],
                "当前结局": slot["结局"],
                "台账当时缺口标": lrow["台账缺口标"],
                "当前缺口标": slot["缺口标"],
                "切流状态": slot.get("切流状态", ""),
                "切流成功": now_success,
                "证据": {
                    "探针": bool(ev.get("探针过线")),
                    "名册": bool(ev.get("名册已切")),
                    "事件": bool(ev.get("切流事件")),
                    "审计": bool(ev.get("审计切流")),
                },
                "缺证": ev.get("缺证", []),
                "假绿": claims_success and not now_success,
            })

        delta = cutover_delta(conn, threshold)
        timeline = audit_timeline(conn, threshold)
        double_book = roster_vs_release_env(conn, threshold)
        fake_green = [r for r in switch_event_replay if r["假绿"]]
        diverged_audits = [t for t in timeline if t["分叉"]]

        return {
            "策略": POLICY_LABEL,
            "阈值": threshold,
            "探针失败次数阈值": threshold,
            "失败次数阈值": threshold,
            "发版统计": release_stats,
            "晋升事件数": promote_count,
            "审计切流记录数": switch_count,
            "审计切流记录": switch_audits,
            "切流台账数": ledger_count,
            "名册与实例": version_vs_instance,
            "缺口台账": gap_ledger,
            "红灯已切流": red_switched,
            "在途发版": active,
            # 切流与回滚交错半截的收束证据。
            "切流差额": delta,
            "切流差额数": len(delta),
            "切流事件重放": switch_event_replay,
            "假切流成功": fake_green,
            "假切流成功数": len(fake_green),
            "审计时序": timeline,
            "审计分叉": diverged_audits,
            "审计分叉数": len(diverged_audits),
            "发版详情环境列": double_book,
            "发版详情双账数": len([d for d in double_book if d["双账"]]),
        }


@router.post("/对账/重放")
@router.post("/对账/修复")
@router.post("/结局/重放")
def replay():
    """幂等重放统一结局，并把半截无实例挂接收敛为可探的『探针待报』行。"""
    with db() as conn:
        report = replay_outcomes(conn)
        audit(
            conn, "对账", None, "结局重放",
            f"补挂接{report['补挂接数']}行 重放台账{report['重放台账数']}条 缺口{report['缺口数']}项",
        )
        return report
