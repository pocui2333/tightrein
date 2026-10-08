from __future__ import annotations

import json
from pathlib import Path

from tightrein.implement.check.runtime import page_runner, pages
from tightrein.implement.check.runtime.page_runner import CaseResult, PageFailure, PageRun
from tightrein.implement.check.runtime.verdict import Result

ROUTES = [{"path": "/orders/:id", "componentFile": "web/OrderDetail.tsx", "spec": "admin/order-detail.spec.ts"},
          {"path": "/users", "componentFile": "web/Users.tsx", "spec": None}]


def _case(title: str, *, pages_seen: list[str], shots: tuple[str, ...], file: str = "common/x.spec.ts") -> CaseResult:
    return CaseResult(title, file, "patrol-admin", "admin", "expected", None, None, shots,
                      ({"pages": pages_seen},))


def test_scope_adds_pages_from_the_changed_files(tmp_path: Path) -> None:
    (tmp_path / "routes.json").write_text(json.dumps(ROUTES))
    inventory = pages.routes(tmp_path, "routes.json")
    assert pages.affected_pages({"affectedPages": ["/orders"]}, ["web/OrderDetail.tsx"], inventory) == [
        "/orders", "/orders/:id"]
    assert pages.routes(tmp_path, None) == []


def test_page_patrol_fails_only_on_failures_of_the_affected_pages() -> None:
    run = PageRun(page_runner.OK, (PageFailure("/orders/42", "console-error", "TypeError", "c1"),
                                   PageFailure("/users", "case-failure", "超时", "c2")))
    item, _ = pages.judge(run, ["/orders/:id"], ROUTES, ())
    # 页面路径规范化后比较：/orders/42 即 /orders/:id
    assert item.result is Result.FAILED and "/orders/42" in (item.reason or "") and "/users" not in (item.reason or "")
    item, _ = pages.judge(run, ["/settings"], ROUTES, ())
    assert item.result is Result.PASSED
    partial, _ = pages.judge(PageRun(page_runner.PARTIAL, notes=("角色 admin 登录失败",)), ["/settings"], ROUTES, ())
    assert partial.result is Result.WEAK
    skipped, shots = pages.judge(PageRun(page_runner.FAILED, notes=("Playwright 未安装",)), ["/settings"], ROUTES, ())
    assert skipped.result is Result.UNVERIFIED and shots == []


def test_screenshots_come_from_cases_on_the_affected_pages() -> None:
    cases = [_case("详情", pages_seen=["http://x/orders/7"], shots=("/raw/a/detail.png",)),
             _case("用户", pages_seen=["http://x/users"], shots=("/raw/b/users.png",)),
             _case("按 spec", pages_seen=[], shots=("/raw/c/spec.png",), file="/ws/e2e/admin/order-detail.spec.ts"),
             _case("名字相近", pages_seen=[], shots=("/raw/d/other.png",),
                   file="/ws/e2e/admin/other-order-detail.spec.ts")]
    shots = pages.screenshots(cases, {"/orders/:id": "/orders/:id"}, ROUTES)
    assert sorted((shot.path, shot.page) for shot in shots) == [("/raw/a/detail.png", "/orders/:id"),
                                                                 ("/raw/c/spec.png", "/orders/:id")]
