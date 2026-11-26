from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from app import fixture_runner

router = APIRouter(prefix="/夹具", tags=["夹具"])


class SuiteIn(BaseModel):
    并发轮数: int = 1


class ConcurrentIn(BaseModel):
    轮数: int = 1
    红灯并发数: int | None = None


@router.post("/切流后打红")
def red_after_switch():
    return fixture_runner.scenario_red_after_switch()


@router.post("/红灯仍放行")
def red_while_released():
    return fixture_runner.scenario_red_while_released()


@router.post("/半截挂接")
def half_attach():
    return fixture_runner.scenario_half_attach()


@router.post("/并发翻转")
def concurrent_flip(body: ConcurrentIn | None = None):
    body = body or ConcurrentIn()
    return fixture_runner.scenario_concurrent_flip(
        rounds=max(1, body.轮数), red_workers=body.红灯并发数
    )


@router.post("/套件")
def suite(body: SuiteIn | None = None):
    body = body or SuiteIn()
    return fixture_runner.run_suite(concurrent_rounds=max(1, body.并发轮数))
