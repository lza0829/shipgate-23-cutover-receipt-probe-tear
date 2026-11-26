from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list
from app.services.outcomes import audit_timeline, fail_threshold

router = APIRouter(tags=["审计"])


@router.get("/审计")
def list_audit(类别: str | None = None, 关键词: str | None = None, 关联编号: int | None = None):
    sql = """
        SELECT id AS 编号, kind AS 类别, ref_id AS 关联编号,
               action AS 动作, created_at AS 时间, note AS 备注
        FROM audit_events
        WHERE 1=1
    """
    params: list = []
    if 类别:
        sql += " AND kind = ?"
        params.append(类别)
    if 关联编号 is not None:
        sql += " AND ref_id = ?"
        params.append(关联编号)
    if 关键词:
        sql += " AND (action LIKE ? OR note LIKE ?)"
        like = f"%{关键词}%"
        params.extend([like, like])
    sql += " ORDER BY id DESC LIMIT 500"
    with db() as conn:
        return rows_to_list(conn.execute(sql, params).fetchall())


@router.get("/审计/时序")
def audit_timeline_view(关联编号: int | None = None):
    """切流审计的『当时结局』与『当前结局』同屏对读，揭穿审计分叉（假切流回执）。"""
    with db() as conn:
        timeline = audit_timeline(conn, fail_threshold(conn))
        if 关联编号 is not None:
            timeline = [t for t in timeline if t["关联编号"] == 关联编号]
        diverged = [t for t in timeline if t["分叉"]]
        return {
            "时序": timeline,
            "分叉": diverged,
            "分叉数": len(diverged),
            "记录数": len(timeline),
        }
