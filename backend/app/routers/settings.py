from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import db, rows_to_list
from app.services.common import audit, now_str
from app.services.outcomes import CANONICAL_THRESHOLD_KEY, looks_like_threshold_key

router = APIRouter(tags=["设置"])


class SettingIn(BaseModel):
    键: str
    值: str


@router.get("/设置")
def list_settings():
    with db() as conn:
        return rows_to_list(
            conn.execute("SELECT key AS 键, value AS 值 FROM settings ORDER BY key").fetchall()
        )


@router.post("/设置")
def upsert_setting(body: SettingIn):
    if not body.键:
        raise HTTPException(400, "键不能为空")
    with db() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (body.键, body.值),
        )
        # 阈值别名写口统一收口：写任一已知阈值键，都同步 canonical 键，
        # 避免“设置页改了『失败次数阈值』而判定仍读『探针失败阈值=3』”。
        canonical_synced = None
        if body.键 != CANONICAL_THRESHOLD_KEY and looks_like_threshold_key(body.键):
            digits = str(body.值).strip()
            if digits.isdigit() and int(digits) >= 1:
                conn.execute(
                    """
                    INSERT INTO settings(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (CANONICAL_THRESHOLD_KEY, digits),
                )
                canonical_synced = digits
        note = body.值
        if canonical_synced is not None:
            note = f"{body.值}（已同步{CANONICAL_THRESHOLD_KEY}={canonical_synced}）"
        audit(conn, "设置", None, body.键, note)
        return {
            "键": body.键,
            "值": body.值,
            "同步键": CANONICAL_THRESHOLD_KEY if canonical_synced is not None else None,
            "同步值": canonical_synced,
            "时间": now_str(),
        }
