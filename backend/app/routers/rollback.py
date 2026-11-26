from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import db, row_to_dict
from app.services.common import (
    active_rollback_window,
    audit,
    get_release,
    now_str,
    set_app_env_version,
    upsert_instance,
)
from app.services.outcomes import (
    CUTOVER_ROLLED_BACK,
    PROD_ENV_CODE,
    evaluate_slot,
    fail_threshold,
    upsert_ledger,
    enrich_release,
)

router = APIRouter(tags=["回滚"])


class RollbackIn(BaseModel):
    目标版本: str
    备注: str = ""
    # 仅夹具使用：'跳过名册' 模拟回滚事件/审计已出、名册环境列尚未切回
    # 的『半截回滚旁路』，与切流写口并发时必须能被揭穿而不是假绿。
    故障注入: str | None = ""


@router.post("/发版单/{release_id}/回滚")
def rollback(release_id: int, body: RollbackIn):
    fault = (body.故障注入 or "").strip()
    if fault not in ("", "跳过名册"):
        raise HTTPException(400, f"未知故障注入：{fault}")
    with db() as conn:
        r = conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
        if not r:
            raise HTTPException(404, "发版单不存在")
        # 半截回滚旁路（名册尚未切回）允许对『已回滚』单再次收束；
        # 已正常收束（名册已切回）的不重复回滚。
        if r["status"] == "已回滚":
            roster = conn.execute(
                "SELECT version FROM app_env_versions WHERE app_id=? AND env_id=?",
                (r["app_id"], r["current_env_id"]),
            ).fetchone()
            already_done = (
                roster is not None
                and (roster["version"] or "") == (body.目标版本 or "")
                and fault == ""
            )
            if already_done:
                raise HTTPException(400, "回滚已收束，无需重复回滚")
        elif r["status"] not in ("已放行", "晋升中"):
            raise HTTPException(400, "当前状态不可回滚")
        if not r["current_env_id"]:
            raise HTTPException(400, "无当前环境，无法回滚")
        win = active_rollback_window(conn, r["current_env_id"])
        if not win:
            raise HTTPException(400, "回滚窗已关，禁止回滚")
        now = now_str()
        # 关键：回滚目标独立成列，绝不改写 target_version——否则发版详情
        # 「目标版本/环境列」会与名册当前列长期双账，切流灯无法重放。
        conn.execute(
            """
            UPDATE releases
            SET status='已回滚', rolled_back_at=?, rollback_version=?
            WHERE id=?
            """,
            (now, body.目标版本, release_id),
        )
        if fault != "跳过名册":
            set_app_env_version(conn, r["app_id"], r["current_env_id"], body.目标版本)
            upsert_instance(
                conn, r["app_id"], r["current_env_id"], body.目标版本, "探针绿", release_id
            )
        # 旧版本实例标红，便于对账发现残留；跨环旁读（别的环境同版本行）
        # 不在此改，单独由健康/对照面按本行版本核对。
        conn.execute(
            """
            UPDATE instances SET health='探针红', updated_at=?
            WHERE app_id=? AND env_id=? AND version!=? AND release_id=?
            """,
            (now, r["app_id"], r["current_env_id"], body.目标版本, release_id),
        )

        threshold = fail_threshold(conn)
        env_code = conn.execute(
            "SELECT code FROM environments WHERE id=?", (r["current_env_id"],)
        ).fetchone()
        env_code = env_code["code"] if env_code else ""
        # 收束本单所有切流台账：前向切流不得再亮成功灯（冻结/已回滚）。
        for row in conn.execute(
            "SELECT switch_key FROM switch_ledger WHERE release_id=?", (release_id,)
        ).fetchall():
            slot = evaluate_slot(
                conn, r["app_id"], r["current_env_id"],
                version=r["target_version"], threshold=threshold,
            )
            upsert_ledger(
                conn,
                switch_key=row["switch_key"],
                release_id=release_id,
                app_id=r["app_id"],
                env_id=r["current_env_id"],
                version=r["target_version"],
                attached_count=slot["匹配实例数"],
                slot=slot,
                created=False,
                state=CUTOVER_ROLLED_BACK,
            )

        audit(
            conn, "回滚", release_id, f"回滚到{body.目标版本}",
            (
                f"原切流目标={r['target_version']} 回滚版本={body.目标版本} "
                f"名册已切回={int(fault != '跳过名册')} "
                f"切流状态={CUTOVER_ROLLED_BACK}"
                + (" 故障注入=跳过名册" if fault == "跳过名册" else "")
                + (f" 备注={body.备注}" if body.备注 else "")
            ),
        )
        release = row_to_dict(get_release(conn, release_id))
        enrich_release(conn, release, threshold)
        release["回滚收束"] = {
            "切流状态": CUTOVER_ROLLED_BACK,
            "切流成功": False,
            "原切流目标": r["target_version"],
            "回滚版本": body.目标版本,
            "名册已切回": fault != "跳过名册",
            "故障注入": fault or "",
        }
        return release
