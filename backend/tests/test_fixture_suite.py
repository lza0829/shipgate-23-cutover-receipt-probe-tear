"""放行↔探针统一结局的可复现夹具测试。

核心用例直接调用 fixture_runner（只依赖 fastapi/pydantic 环境）；
HTTP 层测试在缺少 httpx 时自动 skip，不会污染收集。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture(scope="module", autouse=True)
def isolated_db():
    """把整个模块重定向到独立临时库，结束后还原，避免污染外部 SHIPGATE_DB。"""
    from app import db as db_mod

    original = db_mod.DB_PATH
    tmp = Path(tempfile.mkdtemp(prefix="shipgate-pytest-")) / "suite.db"
    db_mod.DB_PATH = tmp
    try:
        yield tmp
    finally:
        db_mod.DB_PATH = original


def test_full_fixture_suite_passes():
    from app.fixture_runner import run_suite

    report = run_suite(concurrent_rounds=2)
    failed = [c for c in report["用例"] if not c.get("通过")]
    assert report["全部通过"], [
        (c.get("标识"), c.get("异常"),
         [a for a in c.get("断言", []) if not a.get("通过")])
        for c in failed
    ]


def test_red_after_switch_separately_reproduced():
    from app.fixture_runner import scenario_red_after_switch

    r = scenario_red_after_switch()
    assert r["通过"], [a for a in r["断言"] if not a["通过"]]
    # 关键产物必须齐备
    art = r["产物"]
    for key in ("放行回执", "名册产版本", "实例健康行", "探针失败次数",
                "对账缺口标", "总览红灯已切流露口", "审计切流记录"):
        assert key in art, f"缺少产物 {key}"


def test_red_while_released_separately_reproduced():
    from app.fixture_runner import scenario_red_while_released

    r = scenario_red_while_released()
    assert r["通过"], r.get("异常")
    assert r["产物"]["放行回执"]["状态"] == "已放行"
    assert r["产物"]["放行回执"]["缺口标"] == "缺口·红灯超标"


def test_half_attach_converges_and_replay_is_idempotent():
    from app.fixture_runner import scenario_half_attach

    r = scenario_half_attach()
    assert r["通过"], [a for a in r["断言"] if not a["通过"]]


def test_concurrent_flip_is_reproducible():
    from app.fixture_runner import scenario_concurrent_flip

    r = scenario_concurrent_flip(rounds=3)
    assert r["通过"], [a for a in r["断言"] if not a["通过"]]


def test_http_fixture_endpoints_via_testclient():
    httpx = pytest.importorskip("httpx")  # TestClient 依赖
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.post("/api/夹具/切流后打红")
        assert resp.status_code == 200
        assert resp.json()["通过"] is True

        resp = client.post("/api/夹具/套件", json={"并发轮数": 1})
        assert resp.status_code == 200
        assert resp.json()["全部通过"] is True
