from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import db, row_to_dict
from app.services.common import (
    active_release_window,
    audit,
    env_by_code,
    get_release,
    get_setting,
    now_str,
    ordered_envs,
    set_app_env_version,
)
from app.services.outcomes import (
    POLICY_LABEL,
    attach_switch_instance,
    enrich_release,
    evaluate_slot,
    fail_threshold,
    upsert_ledger,
)
router = APIRouter(tags=["晋升"])


class PromoteIn(BaseModel):
    目标环境: str
    备注: str = ""
    # 仅夹具使用：'跳过挂接' 模拟切流/发版单/名册已提交、实例行尚未挂接的半截崩溃；
    # '跳过名册' 模拟切流回执/事件/审计已出，但名册环境列尚未切（名册未切路径）。
    故障注入: str | None = ""


@router.post("/发版单/{release_id}/晋升")
def promote(release_id: int, body: PromoteIn):
    fault = (body.故障注入 or "").strip()
    if fault not in ("", "跳过挂接", "跳过名册"):
        raise HTTPException(400, f"未知故障注入：{fault}")

    with db() as conn:
        r = conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
        if not r:
            raise HTTPException(404, "发版单不存在")
        if r["status"] not in ("已入闸", "晋升中"):
            raise HTTPException(400, "当前状态不可晋升")
        to_env = env_by_code(conn, body.目标环境)
        envs = ordered_envs(conn)
        codes = [e["code"] for e in envs]
        if body.目标环境 not in codes:
            raise HTTPException(400, "目标环境非法")
        cur_env = None
        if r["current_env_id"]:
            cur_env = conn.execute(
                "SELECT * FROM environments WHERE id=?", (r["current_env_id"],)
            ).fetchone()
        allow_skip = get_setting(conn, "允许跳级", "否") == "是"
        if cur_env:
            cur_idx = codes.index(cur_env["code"])
            to_idx = codes.index(body.目标环境)
            if to_idx <= cur_idx:
                raise HTTPException(400, "只能向更高环境晋升")
            if not allow_skip and to_idx != cur_idx + 1:
                raise HTTPException(400, "禁止跳级，须逐级晋升")
        else:
            if body.目标环境 != "测":
                raise HTTPException(400, "未落地时只能先入测")

        win = active_release_window(conn, to_env["id"])
        hard = get_setting(conn, "维护窗硬拒", "是") == "是"
        if hard and not win:
            raise HTTPException(400, "目标环境放行窗已关，禁止晋升")

        is_prod = body.目标环境 == "产"
        now = now_str()
        conn.execute(
            """
            INSERT INTO promote_events(release_id, from_env_id, to_env_id, decision, created_at, note)
            VALUES (?, ?, ?, '放行', ?, ?)
            """,
            (release_id, r["current_env_id"], to_env["id"], now, body.备注),
        )
        new_status = "已放行" if is_prod else "晋升中"
        released_at = now if is_prod else r["released_at"]
        conn.execute(
            """
            UPDATE releases SET status=?, current_env_id=?, released_at=? WHERE id=?
            """,
            (new_status, to_env["id"], released_at, release_id),
        )

        # 1) 名册切流登记（方案二：允许先切，不被探针颜色阻塞）。
        #    夹具可注入“跳过名册”，复现『事件/审计已出、名册环境列未切』。
        if fault != "跳过名册":
            set_app_env_version(conn, r["app_id"], to_env["id"], r["target_version"])

        # 2) 实例挂接。正常路径同事务挂上；夹具可注入“半截无实例挂接”。
        instance_id = None
        if fault != "跳过挂接":
            instance_id = attach_switch_instance(
                conn, r["app_id"], to_env["id"], r["target_version"], release_id
            )

        threshold = fail_threshold(conn)
        # 核对版本固定为本单目标版本：即使名册被注入『跳过名册』而未切，
        # 也要按目标版本算探针证据，才能把『名册未切』这一路显形。
        slot = evaluate_slot(
            conn, r["app_id"], to_env["id"], version=r["target_version"],
            threshold=threshold,
        )
        attached_count = slot["匹配实例数"]

        # 3) 切流台账（生产放行即切流；其余环境也留晋升结局快照）。
        switch_key = f"r{release_id}-e{to_env['id']}"
        ledger_id = upsert_ledger(
            conn,
            switch_key=switch_key,
            release_id=release_id,
            app_id=r["app_id"],
            env_id=to_env["id"],
            version=r["target_version"],
            attached_count=attached_count,
            slot=slot,
            created=True,
        )

        audit(conn, "晋升", release_id, f"晋升到{body.目标环境}", body.备注)
        if is_prod:
            # 明示的审计切流记录：与放行回执同一结局/缺口标/证据，便于重放对读。
            ev = slot.get("切流证据") or {}
            audit(
                conn, "切流", release_id, "切流登记",
                (
                    f"{r['serial']} 生产切流 名册产版本={r['target_version']} "
                    f"结局={slot['结局']} 缺口标={slot['缺口标'] or '无'} "
                    f"阈值={threshold} 挂接实例={attached_count} "
                    f"切流状态={slot['切流状态']} "
                    f"探针过线={int(bool(ev.get('探针过线')))} "
                    f"名册已切={int(bool(ev.get('名册已切')))} "
                    f"事件同核={int(bool(ev.get('切流事件')))} "
                    f"审计=本行落库 "
                    f"切流成功={int(bool(slot['切流成功']))}"
                    + (" 故障注入=跳过挂接" if fault == "跳过挂接" else "")
                    + (" 故障注入=跳过名册" if fault == "跳过名册" else "")
                    + (f" 备注={body.备注}" if body.备注 else "")
                ),
                # 审计必须与实际名册当前列同核；跳过名册时名册尚未切，
                # 这里如实记下『名册未切』而不是等探针红了才发现。
            )

        # 4) 审计落库后重评一次：成功灯以探针∪名册∪事件∪审计四件同核为准。
        slot = evaluate_slot(
            conn, r["app_id"], to_env["id"], version=r["target_version"],
            threshold=threshold,
        )
        attached_count = slot["匹配实例数"]
        upsert_ledger(
            conn,
            switch_key=switch_key,
            release_id=release_id,
            app_id=r["app_id"],
            env_id=to_env["id"],
            version=r["target_version"],
            attached_count=attached_count,
            slot=slot,
            created=False,
        )

        release = row_to_dict(get_release(conn, release_id))
        enrich_release(conn, release, threshold)

        ledger = conn.execute(
            "SELECT * FROM switch_ledger WHERE id=?", (ledger_id,)
        ).fetchone()
        evidence = slot.get("切流证据") or {}
        receipt = {
            "回执号": f"HZ-{release_id}-{to_env['code']}",
            "切流台账编号": ledger_id,
            "策略": POLICY_LABEL,
            "发版单编号": release_id,
            "发版单号": r["serial"],
            "状态": new_status,
            "目标环境": body.目标环境,
            "名册产版本": slot["名册版本"] if is_prod else "",
            "名册版本": slot["名册版本"],
            "实例编号": instance_id,
            "故障注入": fault or "",
            "切流登记": row_to_dict(ledger),
            "结局": slot["结局"],
            "缺口标": slot["缺口标"],
            "是否缺口": slot["是否缺口"],
            "红灯已切流": slot["红灯已切流"],
            "切流状态": slot["切流状态"],
            "切流成功": slot["切流成功"],
            "证据缺口标": slot["证据缺口标"],
            "缺证": slot["缺证"],
            "证据": {
                "探针": bool(evidence.get("探针过线")),
                "名册": bool(evidence.get("名册已切")),
                "事件": bool(evidence.get("切流事件")),
                "审计": bool(evidence.get("审计切流")),
            },
            "阈值": threshold,
            "探针失败阈值": threshold,
            "探针失败次数阈值": threshold,
            "失败次数阈值": threshold,
            "实例总数": slot["实例总数"],
            "绿数": slot["绿数"],
            "红灯数": slot["红灯数"],
            "待报数": slot["待报数"],
            "超标数": slot["超标数"],
            "失败次数": slot["失败次数"],
            "实例健康行": slot["实例"],
        }
        release["放行回执"] = receipt
        return release
