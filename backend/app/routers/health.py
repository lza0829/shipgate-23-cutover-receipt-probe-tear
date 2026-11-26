from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import db
from app.services.common import audit, now_str
from app.services.outcomes import (
    INSTANCE_SELECT,
    evaluate_slot,
    fail_threshold,
    query_instance_views,
    refresh_ledger_for_version,
    view_instance,
)
from app.services import cutover_surface

router = APIRouter(tags=["健康"])


class HealthIn(BaseModel):
    健康: str


class InstanceIn(BaseModel):
    环境: str
    版本: str
    应用编号: int | None = None
    应用: str | None = None
    健康: str = "探针绿"
    失败次数: int | None = None
    备注: str = ""


@router.get("/实例")
@router.get("/实例册")
def list_instances():
    with db() as conn:
        return query_instance_views(conn)


@router.post("/实例/登记")
def register_instance(body: InstanceIn):
    if body.健康 not in ("探针绿", "探针红", "探针待报"):
        raise HTTPException(400, "健康状态非法")
    if not body.版本:
        raise HTTPException(400, "版本不能为空")
    with db() as conn:
        if body.应用编号 is not None:
            app = conn.execute("SELECT * FROM apps WHERE id=?", (body.应用编号,)).fetchone()
        elif body.应用:
            app = conn.execute("SELECT * FROM apps WHERE name=?", (body.应用,)).fetchone()
        else:
            raise HTTPException(400, "必须提供应用编号或应用名")
        if not app:
            raise HTTPException(404, "应用不存在")
        env = conn.execute("SELECT * FROM environments WHERE code=?", (body.环境,)).fetchone()
        if not env:
            raise HTTPException(400, f"环境不存在：{body.环境}")

        if body.失败次数 is not None:
            fail_count = max(0, body.失败次数)
        elif body.健康 == "探针红":
            fail_count = 1
        else:
            fail_count = 0

        now = now_str()
        existing = conn.execute(
            "SELECT * FROM instances WHERE app_id=? AND env_id=? AND version=?",
            (app["id"], env["id"], body.版本),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE instances SET health=?, fail_count=?, updated_at=? WHERE id=?",
                (body.健康, fail_count, now, existing["id"]),
            )
            iid = existing["id"]
        else:
            cur = conn.execute(
                """
                INSERT INTO instances
                  (app_id, env_id, version, health, release_id, updated_at,
                   fail_count, backfilled)
                VALUES (?, ?, ?, ?, NULL, ?, ?, 0)
                """,
                (app["id"], env["id"], body.版本, body.健康, now, fail_count),
            )
            iid = cur.lastrowid

        threshold = fail_threshold(conn)
        slot = evaluate_slot(conn, app["id"], env["id"], version=body.版本, threshold=threshold)
        refresh_ledger_for_version(conn, app["id"], env["id"], body.版本, threshold)
        audit(
            conn, "实例", iid, "实例登记",
            f"{app['name']} {body.环境} {body.版本} {body.健康} 连续失败{fail_count}/{threshold} {body.备注}".strip(),
        )
        row = conn.execute(INSTANCE_SELECT + " WHERE i.id=?", (iid,)).fetchone()
        return view_instance(row, threshold, slot)


@router.post("/实例/{instance_id}/健康")
def set_health(instance_id: int, body: HealthIn):
    if body.健康 not in ("探针绿", "探针红", "探针待报"):
        raise HTTPException(400, "健康状态非法")
    with db() as conn:
        row = conn.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
        if not row:
            raise HTTPException(404, "实例不存在")
        threshold = fail_threshold(conn)
        now = now_str()

        # 原子自增：并发打红不会丢失败次数；转绿才清零。
        if body.健康 == "探针红":
            conn.execute(
                "UPDATE instances SET health='探针红', fail_count=fail_count+1, updated_at=? WHERE id=?",
                (now, instance_id),
            )
        elif body.健康 == "探针绿":
            conn.execute(
                "UPDATE instances SET health='探针绿', fail_count=0, updated_at=? WHERE id=?",
                (now, instance_id),
            )
        else:
            conn.execute(
                "UPDATE instances SET health='探针待报', fail_count=0, updated_at=? WHERE id=?",
                (now, instance_id),
            )

        fresh = conn.execute(INSTANCE_SELECT + " WHERE i.id=?", (instance_id,)).fetchone()
        fc = fresh["fail_count"]
        crossed = body.健康 == "探针红" and fc == threshold
        slot = evaluate_slot(
            conn, fresh["应用编号"], fresh["环境编号"],
            version=fresh["版本"], threshold=threshold,
        )
        refresh_ledger_for_version(
            conn, fresh["应用编号"], fresh["环境编号"], fresh["版本"], threshold
        )
        note = f"连续失败 {fc}/{threshold} 次"
        if crossed:
            note += "；已达失败阈值，缺口升级为红灯超标"
        note += f"；统一结局={slot['结局']} 缺口标={slot['缺口标'] or '无'}"
        audit(conn, "探针", instance_id, body.健康, note)

        return view_instance(fresh, threshold, slot)

def _health_panel_cutover23(conn=None):
    if conn is None:
        return {"panel": "cutover23"}
    try:
        last = cutover_surface.get_json(conn, "surface_last", {}) or {}
        return {"panel": "cutover23", "release_id": last.get("release_id"), "ok": bool(last.get("ok"))}
    except Exception:
        return {"panel": "cutover23"}

