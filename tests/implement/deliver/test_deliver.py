from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tightrein.assess.issue.transitions import IssueEvent
from tightrein.implement.deliver import deliver
from tightrein.protocol.handoff import Status, check_facts, load_schema, write
from tightrein.protocol.naming import FileName
from tightrein.release.record import parse_delivery

FIX = {"src/orders.py": "def recent(days):\n    return days * 7\n", "src/week.py": "DAYS = 7\n"}
RELEASE = {"prTitle": "订单列表显示最近 7 天", "scope": "orders", "subject": "按 7 天查询订单", "why": "只显示当天",
           "problem": "查询条件写成了当天", "approach": "改为最近 7 天", "limitations": None}


def _latest(world: Any, *, check_hash: str | None = None, review_hash: str | None = None,
            review_status: Status = Status.PASSED) -> dict[str, Any]:
    diff_hash = world.git().diff_hash(world.repo.base)
    check = world.handoff("implement.check", {
        "diffHash": check_hash or diff_hash,
        "commands": [{"name": "test", "command": "pytest -q", "result": "passed", "reason": None},
                     {"name": "lint", "command": "ruff check .", "result": "passed", "reason": None}],
        "runtime": [{"id": "api:shallow", "category": "api", "result": "passed", "reason": None},
                    {"id": "page:/orders", "category": "pages", "result": "weak", "reason": "测试库没有订单数据"},
                    {"id": "screenshot:orders.png", "category": "screenshots", "result": "unverified",
                     "reason": "页面服务没有启动"}]})
    review = world.handoff("implement.review", {
        "diffHash": review_hash or diff_hash,
        "acceptance": [{"criterion": "列表显示最近 7 天的订单", "result": "pass", "reason": "看了查询条件"}],
        "unverified": [{"item": "接口 GET /api/orders 返回 200", "reason": "用户判断：本机看过"}]}, review_status)
    review.round = 2
    code = world.handoff("implement.code", {"release": RELEASE})
    return {"implement.check": check, "implement.review": review, "implement.code": code}


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    found: list[tuple[Any, ...]] = []

    def apply_event(runtime: Any, issue: str, event: IssueEvent, **options: Any) -> None:
        found.append((issue, event, options))

    monkeypatch.setattr(deliver, "apply_event", apply_event)
    return found


def test_delivery_writes_every_fact_the_release_reads(world: Any, events: list[tuple[Any, ...]]) -> None:
    world.repo.write(FIX)
    result = deliver.run(world.runtime, world.context(round=2, latest=_latest(world)))
    assert result.status is Status.PASSED and result.point == "implement.deliver"
    facts = result.facts
    check_facts("implement.deliver", facts, load_schema(deliver.SCHEMA))
    assert facts["branch"] == "fix/18-orders" and facts["worktree"] == str(world.repo.path)
    assert facts["commit"] == world.repo.base and facts["base"] == world.repo.base
    assert facts["diffHash"] == world.git().diff_hash(world.repo.base) and facts["skipped"] is None
    changed = {item["path"]: item for item in facts["changedFiles"]}
    assert set(changed) == {"src/orders.py", "src/week.py"} and changed["src/week.py"]["status"] == "?"
    assert facts["review"] == {"round": 2, "conclusions": ["implement.review 的结论",
                                                           "验收标准「列表显示最近 7 天的订单」：pass(看了查询条件)"]}
    assert facts["release"] == {"title": "订单列表显示最近 7 天", "scope": "orders", "summary": "按 7 天查询订单",
                                "why": "只显示当天", "problem": "查询条件写成了当天", "approach": "改为最近 7 天",
                                "limitations": None}
    assert facts["acceptedFindings"] == [] and facts["highRiskPaths"] == [] and facts["highRisk"] is False
    # 发布按 release/record.parse_delivery 读，键名一致
    delivery = parse_delivery(facts, "0018")
    assert delivery.branch == "fix/18-orders" and delivery.commit == world.repo.base
    assert delivery.changed_files == ("src/orders.py", "src/week.py") and delivery.review_round == 2
    assert delivery.text.title == "订单列表显示最近 7 天" and delivery.checks_passed
    assert [event for _, event, _ in events] == [IssueEvent.DELIVER]
    assert events[0][2]["updates"] == {"branch": "fix/18-orders"}


def test_the_delivery_sums_up_every_round(world: Any, events: list[tuple[Any, ...]]) -> None:
    world.repo.write(FIX)
    blockers = [{"check": "review", "kind": "hardcode", "location": "src/orders.py:2", "summary": "写死了 7",
                 "category": "local", "trigger": "days 为 3"}]
    for number, point, status, facts in [(1, "implement.code", Status.PASSED, {}),
                                          (1, "implement.review", Status.FAILED, {"blockers": blockers}),
                                          (2, "implement.code", Status.PASSED, {}),
                                          (2, "implement.review", Status.PASSED, {"blockers": []})]:
        handoff = world.handoff(point, facts, status)
        handoff.round = number
        write(world.runtime.workspace.step_file("0018", FileName(point, "handoff", "json", round=number)), handoff)
    facts = deliver.run(world.runtime, world.context(round=2, latest=_latest(world))).facts
    check_facts("implement.deliver", facts, load_schema(deliver.SCHEMA))
    assert [item["round"] for item in facts["rounds"]] == [1, 2]
    first = facts["rounds"][0]["steps"]
    assert set(first) == {"implement.code", "implement.review"}
    assert first["implement.review"] == {"status": "failed", "summary": "implement.review 的结论",
                                         "blockers": ["[local/hardcode] src/orders.py:2：写死了 7"]}
    assert facts["rounds"][1]["steps"]["implement.review"]["status"] == "passed"


def test_weak_evidence_and_unverified_items_are_reported_but_never_written_as_plain_passes(
        world: Any, events: list[tuple[Any, ...]]) -> None:
    world.repo.write(FIX)
    facts = deliver.run(world.runtime, world.context(latest=_latest(world))).facts
    checks = {item["name"]: item for item in facts["checks"]}
    assert checks["test"] == {"name": "test", "passed": True, "detail": None}
    assert checks["api:shallow"]["passed"] is True
    weak = next(name for name in checks if name.startswith("page:/orders"))
    assert "弱证据" in weak and "测试库没有订单数据" in weak
    assert any(name.startswith("screenshot:orders.png") and "未验证" in name for name in checks)
    assert "page:/orders：弱证据(测试库没有订单数据)" in facts["unverified"]
    assert "接口 GET /api/orders 返回 200：用户判断：本机看过" in facts["unverified"]


def test_the_patch_includes_untracked_files(world: Any, events: list[tuple[Any, ...]]) -> None:
    world.repo.write(FIX)
    facts = deliver.run(world.runtime, world.context(latest=_latest(world))).facts
    patch = Path(facts["patch"])
    assert patch.name.endswith("implement.deliver-diff.patch")
    text = patch.read_text(encoding="utf-8")
    assert "src/orders.py" in text and "+DAYS = 7" in text


def test_high_risk_paths_are_listed(new_world: Any, settings_with: Any, events: list[tuple[Any, ...]]) -> None:
    world = new_world(settings=settings_with({"boundaries": {"protected": {"highRisk": ["src/week.py"]}}}))
    world.repo.write(FIX)
    facts = deliver.run(world.runtime, world.context(latest=_latest(world))).facts
    assert facts["highRisk"] is True and facts["highRiskPaths"] == ["src/week.py"]
    assert parse_delivery(facts, "0018").high_risk_paths == ("src/week.py",)


def test_changes_after_the_check_or_the_review_go_back_to_that_step(world: Any,
                                                                    events: list[tuple[Any, ...]]) -> None:
    world.repo.write(FIX)
    stale = "0" * 64
    back = deliver.run(world.runtime, world.context(latest=_latest(world, check_hash=stale)))
    assert back.status is Status.FAILED and back.facts["backTo"] == "implement.check"
    back = deliver.run(world.runtime, world.context(latest=_latest(world, review_hash=stale)))
    # 审查之后又改了：只回到审查，不重新编码
    assert back.status is Status.FAILED and back.facts["backTo"] == "implement.review"
    back = deliver.run(world.runtime, world.context(latest=_latest(world, review_status=Status.PENDING)))
    assert back.facts["backTo"] == "implement.review"
    assert events == []
