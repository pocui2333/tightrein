from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tightrein.collect.api_fuzz.schemathesis.report_parser import Failure, RecordedCase, Report
from tightrein.implement.check.runtime import api
from tightrein.implement.check.runtime.verdict import Result
from tightrein.protocol.http import HttpResponse
from tightrein.store.tables import problems
from tightrein.store.tables.problems import Problem


def _failure(method: str, route: str) -> Failure:
    return Failure((method, route), "not_a_server_error", "Server error", "500", "fuzzing", "c1",
                   RecordedCase(method, route), None)


def test_scope_adds_endpoints_from_the_changed_files(tmp_path: Path) -> None:
    (tmp_path / "endpoints.json").write_text(json.dumps([
        {"method": "get", "route": "/api/orders", "sourceFile": "src/orders.py"},
        {"method": "POST", "route": "/api/users", "sourceFile": "src/users.py"}]))
    inventory = api.inventory(tmp_path, "endpoints.json")
    found = api.affected_endpoints({"affectedEndpoints": ["GET /api/orders/{id}"]}, ["src/orders.py"], inventory)
    assert found == ["GET /api/orders/{id}", "GET /api/orders"]
    assert api.inventory(tmp_path, None) == [] and api.inventory(tmp_path, "missing.json") == []


def test_signals_of_problems_that_existed_before_the_change_do_not_fail(world: Any) -> None:
    conn, clock = world.runtime.conn, world.clock
    for number, (location, issue, status) in enumerate([("GET /api/orders/{id}", None, "ongoing"),
                                                        ("GET /api/users", "0018", "ongoing"),
                                                        ("GET /api/old", None, "resolved")], start=1):
        problems.save(conn, Problem(f"P-000{number}", f"f{number}", api.SOURCE, "server_error", status, "5xx",
                                    clock.now(), clock.now(), location=location, issue=issue), clock)
    known = api.existing(conn, "0018")
    assert known == {"f1": "P-0001", "GET /api/orders/{id}": "P-0001"}
    report = Report(True, failures=(_failure("GET", "/api/orders/{id}"),))
    passed = api.judge(report, "cmd", known, ())
    assert passed.result is Result.PASSED and "P-0001" in (passed.reason or "")
    # 属于本 Issue 的、已解决的都算新出现
    report = Report(True, failures=(_failure("GET", "/api/orders/{id}"), _failure("GET", "/api/users")))
    failed = api.judge(report, "cmd", known, ())
    assert failed.result is Result.FAILED and "GET /api/users" in (failed.reason or "")
    assert api.judge(Report(False, errors=("boom",)), "cmd", known, ()).result is Result.UNVERIFIED


def test_failures_are_matched_by_fingerprint_including_aliases(world: Any) -> None:
    """位置写法不同(问题记的是实际路径)也能按指纹认出；合并来的别名指纹同样算已存在。"""
    conn, clock = world.runtime.conn, world.clock
    merged = api.fingerprint(_failure("GET", "/api/orders/{id}"))
    problems.save(conn, Problem("P-0001", "other-print", api.SOURCE, "server_error", "ongoing", "5xx", clock.now(),
                                clock.now(), location="GET /legacy/orders", extra={"aliases": [merged]}), clock)
    known = api.existing(conn, "0018")
    assert known[merged] == "P-0001"
    passed = api.judge(Report(True, failures=(_failure("GET", "/api/orders/{id}"),)), "cmd", known, ())
    assert passed.result is Result.PASSED and "P-0001" in (passed.reason or "")
    # 指纹与采集产出的信号一致：同一操作同一状态码大类
    assert api.fingerprint(_failure("GET", "/api/orders/{id}")) == merged
    assert api.fingerprint(_failure("POST", "/api/orders/{id}")) != merged


def test_shallow_is_unverified_without_a_service_or_a_spec(world: Any, tmp_path: Path) -> None:
    settings = api.ApiSettings.from_settings(world.runtime.settings)
    common = {"worktree": tmp_path, "report_dir": tmp_path / "api", "settings": settings, "runner": world.runner,
                  "environ": {}, "token": "", "transport": lambda request: HttpResponse(404), "known": {}}
    assert api.shallow([], base_url=None, unavailable=None, **common) is None
    item = api.shallow(["GET /api/orders"], base_url=None, unavailable="api 未就绪：端口被占", **common)
    assert item is not None and item.result is Result.UNVERIFIED and item.reason == "api 未就绪：端口被占"
    item = api.shallow(["GET /api/orders"], base_url="http://localhost:1", unavailable=None, **common)
    assert item is not None and item.result is Result.UNVERIFIED
