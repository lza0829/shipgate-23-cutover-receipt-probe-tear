from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list
from app.services.common import RELEASE_SELECT
from app.services.outcomes import (
    POLICY_LABEL,
    enrich_release,
    fail_threshold,
    query_instance_views,
    roster_views,
)
from app.services import cutover_surface

router = APIRouter(tags=["总览"])


@router.get("/总览")
def overview():
    with db() as conn:
        threshold = fail_threshold(conn)
        counts = {}
        for name in ("待入闸", "已入闸", "晋升中", "已放行", "回滚中", "已回滚", "已关闭"):
            counts[name] = conn.execute(
                "SELECT COUNT(*) AS c FROM releases WHERE status=?", (name,)
            ).fetchone()["c"]
        active = rows_to_list(
            conn.execute(
                RELEASE_SELECT
                + " WHERE r.status IN ('已入闸','晋升中','已放行','回滚中') ORDER BY r.id DESC"
            ).fetchall()
        )
        active = [enrich_release(conn, r, threshold) for r in active]

        roster = roster_views(conn, threshold)
        versions = [
            {
                "应用编号": s["应用编号"],
                "环境编号": s["环境编号"],
                "应用": s["应用"],
                "环境": s["环境"],
                "环境名": s["环境名"],
                "版本": s["名册版本"],
                "名册版本": s["名册版本"],
                "更新时间": s["名册更新时间"],
                "结局": s["结局"],
                "缺口标": s["缺口标"],
                "是否缺口": s["是否缺口"],
                "红灯已切流": s["红灯已切流"],
                "阈值": s["阈值"],
                "探针失败次数阈值": s["探针失败次数阈值"],
                "匹配实例数": s["匹配实例数"],
                "绿数": s["绿数"],
                "红灯数": s["红灯数"],
                "待报数": s["待报数"],
                "失败次数": s["失败次数"],
            }
            for s in roster
        ]
        health = query_instance_views(conn, threshold)

        gap_ledger = [
            {
                "应用编号": s["应用编号"], "环境编号": s["环境编号"],
                "应用": s["应用"], "环境": s["环境"], "环境名": s["环境名"],
                "名册版本": s["名册版本"], "结局": s["结局"], "缺口标": s["缺口标"],
                "红灯已切流": s["红灯已切流"], "匹配实例数": s["匹配实例数"],
                "红灯数": s["红灯数"], "失败次数": s["失败次数"], "阈值": s["阈值"],
                "探针失败次数阈值": s["探针失败次数阈值"],
            }
            for s in roster if s["是否缺口"]
        ]
        # 总览明示露口：名册已切、当前版本探针红（计数中或超标）。
        red_switched = [g for g in gap_ledger if g["红灯已切流"]]
        attach_gaps = [
            g for g in gap_ledger if g["结局"] in ("无实例挂接", "探针待报")
        ]

        return {
            "策略": POLICY_LABEL,
            "阈值": threshold,
            "探针失败阈值": threshold,
            "探针失败次数阈值": threshold,
            "失败次数阈值": threshold,
            "分布": counts,
            "在途": active,
            "环境版本": versions,
            "名册": versions,
            "实例": health,
            "缺口台账": gap_ledger,
            "红灯已切流": red_switched,
            "红灯已切流数": len(red_switched),
            "挂接缺口": attach_gaps,
            "挂接缺口数": len(attach_gaps),
        }

# surface panel hook kept for overview consumers
def _surface_overview_note():
    return "cutover_surface"

