from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import db
from app.services.common import audit, now_str
from app.services.outcomes import (
    INSTANCE_SELECT,
    POLICY_LABEL,
    evaluate_slot,
    fail_threshold,
    query_instance_views,
    refresh_ledger_for_version,
    view_instance,
)
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


@router.get("/探针/对照")
def probe_roster_compare(应用编号: int | None = None, 环境: str | None = None):
    """健康探针字段与名册切流列同屏对读。

    一行同时给出：名册当前版本/环境列、同版本探针行的健康/连续失败、
    切流事件与审计是否同核，以及切流成功灯。前端「健康页」与「切流页」
    都用同一行数据，不得各算一套。
    """
    with db() as conn:
        threshold = fail_threshold(conn)
        sql = """
            SELECT v.app_id AS 应用编号, v.env_id AS 环境编号
            FROM app_env_versions v
            JOIN apps a ON a.id=v.app_id
            JOIN environments e ON e.id=v.env_id
            WHERE 1=1
        """
        params: list = []
        if 应用编号 is not None:
            sql += " AND v.app_id=?"
            params.append(应用编号)
        if 环境:
            sql += " AND e.code=?"
            params.append(环境)
        sql += " ORDER BY a.id, e.sort_order"
        rows = conn.execute(sql, params).fetchall()
        out: list[dict] = []
        for r in rows:
            slot = evaluate_slot(conn, r["应用编号"], r["环境编号"], threshold=threshold)
            ev = slot.get("切流证据") or {}
            probe_rows = slot.get("实例", [])
            out.append({
                "应用编号": r["应用编号"],
                "环境编号": r["环境编号"],
                "应用": slot["应用"],
                "环境": slot["环境"],
                "名册环境列": slot["环境"],
                "名册版本": slot["名册版本"],
                "名册产版本": slot["名册产版本"],
                "名册更新时间": slot["名册更新时间"],
                "核对版本": slot["核对版本"],
                "探针行": [
                    {
                        "实例编号": p["编号"],
                        "版本": p["版本"],
                        "健康": p["健康"],
                        "连续失败次数": p["连续失败次数"],
                        "阈值": p["阈值"],
                        "发版单编号": p["发版单编号"],
                        "补挂": p["挂接补登"],
                    }
                    for p in probe_rows
                ],
                "探针过线": bool(ev.get("探针过线")),
                "名册已切": bool(ev.get("名册已切")),
                "切流事件": bool(ev.get("切流事件")),
                "审计切流": bool(ev.get("审计切流")),
                "缺证": ev.get("缺证", []),
                "结局": slot["结局"],
                "缺口标": slot["缺口标"],
                "证据缺口标": slot.get("证据缺口标", ""),
                "切流状态": slot.get("切流状态", ""),
                "切流成功": bool(slot.get("切流成功")),
                "阈值": threshold,
            })
        return {"对照": out, "阈值": threshold, "策略": POLICY_LABEL}


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
