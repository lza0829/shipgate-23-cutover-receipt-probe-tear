#!/usr/bin/env python3
"""放行↔探针统一结局夹具的可复现入口。

用法：
  # 隔离临时库，进程内直接跑全套（默认；无需起服务）
  python run_fixtures.py

  # 只跑某一套，并发翻转跑 5 轮验证可复现
  python run_fixtures.py --scenario concurrent --rounds 5

  # 对已经启动的真实服务跑（真正 HTTP；可打 docker compose 起的 11800）
  python run_fixtures.py --base-url http://127.0.0.1:11800 --rounds 3

退出码：全部断言通过为 0，否则 1。报告写到 fixtures/report.json（可用 --report 改）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.request
from urllib.parse import quote
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

SCENARIO_PATHS = {
    "red-after-switch": "/api/夹具/切流后打红",
    "red-while-released": "/api/夹具/红灯仍放行",
    "half-attach": "/api/夹具/半截挂接",
    "concurrent": "/api/夹具/并发翻转",
    "suite": "/api/夹具/套件",
}


def _post_http(base_url: str, path: str, payload: dict | None = None) -> dict:
    url = base_url.rstrip("/") + quote(path, safe="/")
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _run_local(scenario: str, rounds: int) -> dict:
    # 隔离临时库，必须在 import app.config 之前设置。
    db_path = Path(tempfile.gettempdir()) / f"shipgate-fixtures-{os.getpid()}.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    os.environ["SHIPGATE_DB"] = str(db_path)

    from app import fixture_runner as fr  # noqa: E402

    if scenario == "suite":
        return fr.run_suite(concurrent_rounds=rounds)
    if scenario == "red-after-switch":
        return fr.scenario_red_after_switch()
    if scenario == "red-while-released":
        return fr.scenario_red_while_released()
    if scenario == "half-attach":
        return fr.scenario_half_attach()
    if scenario == "concurrent":
        return fr.scenario_concurrent_flip(rounds=rounds)
    raise SystemExit(f"未知场景：{scenario}")


def _run_http(base_url: str, scenario: str, rounds: int) -> dict:
    if scenario == "suite":
        return _post_http(base_url, SCENARIO_PATHS["suite"], {"并发轮数": rounds})
    if scenario == "concurrent":
        return _post_http(base_url, SCENARIO_PATHS["concurrent"], {"轮数": rounds})
    return _post_http(base_url, SCENARIO_PATHS[scenario])


def _print_case(case: dict, indent: int = 0) -> None:
    pad = "  " * indent
    mark = "PASS" if case.get("通过") else "FAIL"
    total = case.get("断言总数")
    passed = case.get("断言通过数")
    extra = f" ({passed}/{total})" if isinstance(total, int) else ""
    print(f"{pad}[{mark}] {case.get('夹具') or case.get('标识')}{extra}")
    if case.get("异常"):
        print(f"{pad}  异常: {case['异常']}")
    for a in case.get("断言", []):
        if not a.get("通过"):
            print(f"{pad}  ✗ {a['检查项']}: 期望={a['期望']!r} 实际={a['实际']!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="", help="例如 http://127.0.0.1:11800；不填走进程内临时库")
    ap.add_argument("--scenario", default="suite", choices=list(SCENARIO_PATHS.keys()))
    ap.add_argument("--rounds", type=int, default=1, help="并发翻转/套件中并发用例的轮数")
    ap.add_argument("--report", default=str(BACKEND_DIR / "fixtures" / "report.json"))
    args = ap.parse_args()

    if args.base_url:
        report = _run_http(args.base_url, args.scenario, args.rounds)
    else:
        report = _run_local(args.scenario, args.rounds)

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.scenario == "suite" and "用例" in report:
        print(f"策略：{report.get('策略')}")
        for case in report["用例"]:
            _print_case(case)
        ok = report.get("全部通过")
        print(f"\n全套：{'全部通过 ✓' if ok else '存在失败 ✗'}  报告：{report_path}")
    else:
        _print_case(report)
        ok = report.get("通过")
        print(f"\n报告：{report_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
