from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list
from app.services.common import RELEASE_SELECT
from app.services.outcomes import (
    CUTOVER_BASELINE,
    CUTOVER_FROZEN,
    CUTOVER_OK,
    CUTOVER_ROLLED_BACK,
    CUTOVER_ROSTER_MISSING,
    POLICY_LABEL,
    cutover_delta,
    enrich_release,
    fail_threshold,
    query_instance_views,
    roster_views,
    roster_vs_release_env,
)

router = APIRouter(tags=["总览"])


def _bucket_row(s: dict) -> dict:
    return {
        "应用编号": s["应用编号"], "环境编号": s["环境编号"],
        "应用": s["应用"], "环境": s["环境"], "环境名": s["环境名"],
        "名册版本": s["名册版本"], "切流目标版本": (s.get("切流证据") or {}).get("切流目标版本", ""),
        "结局": s["结局"], "缺口标": s["缺口标"], "是否缺口": s["是否缺口"],
        "红灯已切流": s["红灯已切流"], "匹配实例数": s["匹配实例数"],
        "红灯数": s["红灯数"], "失败次数": s["失败次数"], "阈值": s["阈值"],
        "探针失败次数阈值": s["探针失败次数阈值"],
        "切流状态": s.get("切流状态", ""), "切流成功": bool(s.get("切流成功")),
        "证据缺口标": s.get("证据缺口标", ""), "缺证": s.get("缺证", []),
        "名册已切": bool(s.get("名册已切")), "探针过线": bool(s.get("探针过线")),
        "切流事件": bool(s.get("切流事件")), "审计切流": bool(s.get("审计切流")),
    }


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
                "切流状态": s.get("切流状态", ""),
                "切流成功": bool(s.get("切流成功")),
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
                "切流状态": s.get("切流状态", ""),
                "证据缺口标": s.get("证据缺口标", ""),
                "缺证": s.get("缺证", []),
            }
            for s in roster if s["是否缺口"]
        ]
        # 总览明示露口：名册已切、当前版本探针红（计数中或超标）。
        red_switched = [g for g in gap_ledger if g["红灯已切流"]]
        attach_gaps = [
            g for g in gap_ledger if g["结局"] in ("无实例挂接", "探针待报")
        ]

        # 切流桶：只有四件同核的槽位才计「已切」；冻结/名册未切/已回滚单列，
        # 绝不由回执或审计的单点绿把桶点亮。
        buckets = {
            CUTOVER_OK: [], CUTOVER_FROZEN: [], CUTOVER_ROSTER_MISSING: [],
            CUTOVER_ROLLED_BACK: [], CUTOVER_BASELINE: [],
        }
        for s in roster:
            state = s.get("切流状态", "")
            if state in buckets:
                buckets[state].append(_bucket_row(s))
        in_flight = [r for r in active if r.get("切流状态") in
                     (CUTOVER_FROZEN, CUTOVER_ROSTER_MISSING, CUTOVER_OK)]

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
            "切流桶": {k: len(v) for k, v in buckets.items()},
            "切流已切": buckets[CUTOVER_OK],
            "切流冻结": buckets[CUTOVER_FROZEN],
            "切流名册未切": buckets[CUTOVER_ROSTER_MISSING],
            "切流已回滚": buckets[CUTOVER_ROLLED_BACK],
            "切流基线": buckets[CUTOVER_BASELINE],
            "切流成功数": len(buckets[CUTOVER_OK]),
            "在途切流": in_flight,
            "在途切流数": len(in_flight),
            "切流差额": cutover_delta(conn, threshold),
            "发版详情环境列": roster_vs_release_env(conn, threshold),
        }
