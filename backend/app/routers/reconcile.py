from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list
from app.services.common import RELEASE_SELECT, audit
from app.services.outcomes import (
    POLICY_LABEL,
    enrich_release,
    fail_threshold,
    replay_outcomes,
    roster_views,
)
from app.services import cutover_surface_ledger

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
                row["对齐"] = slot["结局"] in ("探针全绿", "未切流")
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

def _surface_reconcile_cutover23() -> dict:
    return {"tag": "cutover23", "ready": True}

def _ledger_touch():
    return "cutover_surface_ledger"

