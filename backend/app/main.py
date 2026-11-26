from __future__ import annotations
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import PROJECT_NAME
from app.routers import (
    apps,
    audit,
    dashboard,
    environments,
    fixtures,
    health,
    promote,
    reconcile,
    releases,
    rollback,
    settings,
    windows,
)
from app.seed import seed_if_empty
from app.db import db
from app.services.outcomes import replay_outcomes

seed_if_empty()
# 启动即收敛一次：上次进程半截退出留下的“已放行但无实例可探”会被幂等补挂，
# 不会长期停留在无探状态。
with db() as _conn:
    replay_outcomes(_conn)

app = FastAPI(title=PROJECT_NAME)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
for mod in (
    dashboard,
    apps,
    environments,
    releases,
    promote,
    windows,
    rollback,
    health,
    reconcile,
    settings,
    audit,
    fixtures,
):
    app.include_router(mod.router, prefix="/api")


@app.get("/api/健康")
def health_check():
    return {"产品": PROJECT_NAME, "状态": "正常"}

