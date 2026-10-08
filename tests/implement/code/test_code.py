"""编码：只在确认过的方案上改；只写自己的 worktree；越界撤回；结局按性质分类；下一轮续接同一会话。"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tightrein.agents.params import HIGH_RISK, Access
from tightrein.agents.result import CallResult, CallStatus
from tightrein.collect.incidental.handoffs import FINDING_SCHEMA
from tightrein.implement.check.findings import LOCAL, NEEDS_USER, PLAN_GAP
from tightrein.implement.code import code
from tightrein.implement.prompts.code import CONTINUE
from tightrein.protocol.handoff import Status, load_schema, schema_errors

DESIGN, APPROVE, CHECK, REVIEW = "implement.design", "implement.approve", "implement.check", "implement.review"
FIX = {"src/orders.py": "def recent(days):\n    return days * 7\n",
       "tests/test_week.py": "def test_week():\n    pass\n"}
INCIDENTAL = {"file": "src/users.py", "line": 1, "symbol": None, "category": "defect", "confidence": "suspected",
              "evidence": "NAME 写死为 'a'", "text": "用户名写死"}
PLAN = {"summary": "查询改为最近 7 天", "planHash": "a" * 64, "acceptance": ["列表显示最近 7 天的订单"],
        "hypothesis": {"cause": "days 未参与查询", "evidence": [{"location": "src/orders.py:2", "fact": "秘密证据"}],
                       "edits": [{"location": "src/orders.py:2", "change": "按 days 过滤"}]},
        "steps": [{"file": "src/orders.py", "change": "按 days 过滤", "verification": "pytest"}],
        "files": [{"path": "src/orders.py", "isNew": False, "reason": None}], "notDoing": ["不改分页"],
        "frontendDesign": None}


def _output(**changes: Any) -> dict[str, Any]:
    found: dict[str, Any] = {
        "knowledgeSuggestions": ["日期计算放在 recent()"], "analysis": "按方案改了 recent", "status": "completed",
        "changedFiles": ["src/orders.py", "tests/test_week.py"], "testsWritten": ["tests/test_week.py"],
        "verification": [{"command": "pytest -q tests/test_week.py", "output": "1 passed"}], "deviations": [],
        "bigIssue": None, "incidentalFindings": [INCIDENTAL], "outOfScope": [],
        "release": {"scope": "orders", "subject": "订单列表显示最近 7 天", "why": "只显示当天", "prTitle": "显示最近 7 天的订单",
                    "problem": "只显示当天", "approach": "按 days 过滤", "limitations": "没有"}}
    found.update(changes)
    return found


class FakeAsk:
    """依次给出预置结果，并在「模型」运行时按预置改 worktree。"""

    def __init__(self, world: Any, *steps: tuple[dict[str, str | None], dict[str, Any] | None, CallStatus]) -> None:
        self.world = world
        self.steps = list(steps)
        self.requests: list[Any] = []
        self.schema = load_schema(code.SCHEMA)

    def __call__(self, runtime: Any, context: Any, request: Any, usage: Any) -> CallResult:
        self.requests.append(request)
        files, output, status = self.steps.pop(0)
        self.world.repo.write(files)
        if output is not None:
            assert schema_errors(output, self.schema) == []
        return CallResult(status, "claude", "opus", output=output, session_id="S-1",
                          error=None if status is CallStatus.OK else "出错")


def _install(monkeypatch: pytest.MonkeyPatch, world: Any,
             *steps: tuple[dict[str, str | None], dict[str, Any] | None, CallStatus]) -> FakeAsk:
    fake = FakeAsk(world, *steps)
    monkeypatch.setattr(code, "ask", fake)
    return fake


def _context(world: Any, *, round: int = 1, **latest: Any) -> Any:
    found = {DESIGN: world.handoff(DESIGN, PLAN), APPROVE: world.handoff(APPROVE, {"planHash": "a" * 64})}
    found.update({key.replace("_", "."): value for key, value in latest.items()})
    return world.context(round=round, latest=found)


def test_coding_needs_the_confirmed_plan(world: Any) -> None:
    context = world.context(latest={DESIGN: world.handoff(DESIGN, PLAN),
                                    APPROVE: world.handoff(APPROVE, {"planHash": "b" * 64})})
    with pytest.raises(ValueError, match="确认"):
        code.run(world.runtime, context)


def test_a_completed_round_reports_the_change(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, world, (FIX, _output(), CallStatus.OK))
    result = code.run(world.runtime, _context(world))
    assert result.status is Status.PASSED and result.round == 1
    request = fake.requests[0]
    assert request.point == code.POINT and request.access is Access.WRITE and request.resume_session is None
    assert request.allowed_commands == ("pytest -q", "ruff check .")  # `{tests}` 去掉后的前缀也算
    # 编码只拿实施要用的字段：根因假说不带证据
    assert "秘密证据" not in request.variables["plan"] and "按 days 过滤" in request.variables["plan"]
    facts = result.facts
    assert facts["linesAdded"] == 3 and facts["linesDeleted"] == 1 and len(facts["diffHash"]) == 64
    assert facts["diffHash"] == world.git().diff_hash(world.repo.base) and facts["baseCommit"] == world.repo.base
    assert [item["path"] for item in facts["changedFiles"]] == ["src/orders.py", "tests/test_week.py"]
    assert facts["knowledgeSuggestions"] == ["日期计算放在 recent()"] and facts["blockers"] == []
    assert facts["incidentalFindings"] == [INCIDENTAL]
    assert schema_errors(INCIDENTAL, load_schema(FINDING_SCHEMA)) == []
    assert facts["release"]["prTitle"] == "显示最近 7 天的订单" and facts["sessionId"] == "S-1"
    assert result.metrics.files_changed == 1  # 测试文件不计入上限


def test_changes_outside_the_boundaries_are_reverted(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, world, ({**FIX, ".env": "TOKEN=x\n"}, _output(), CallStatus.OK))
    result = code.run(world.runtime, _context(world))
    assert result.status is Status.FAILED and result.facts["violations"][0]["path"] == ".env"
    assert result.facts["blockers"][0]["category"] == NEEDS_USER
    # 本轮改动全部撤回
    assert not (world.repo.path / ".env").exists() and world.git().status().clean


def test_a_redo_after_a_violation_says_why(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, world, (FIX, _output(), CallStatus.OK))
    previous = world.handoff("implement.code", {"violations": [{"kind": "forbidden", "path": ".env",
                                                                  "detail": "禁改文件"}]}, Status.FAILED)
    previous = replace(previous, round=1)
    result = code.run(world.runtime, _context(world, implement_code=previous))
    assert result.facts["redo"] is True
    assert fake.requests[0].variables["corrections"].startswith("- 上一次的改动越界，已全部撤回")
    assert "禁改文件：.env" in fake.requests[0].variables["corrections"]


def test_plan_gaps_and_call_failures_are_classified(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    big = {"description": "recent 被三处调用，要改公共实现", "locations": ["src/orders.py:1"]}
    _install(monkeypatch, world, ({}, _output(status="aborted", bigIssue=big, release=None), CallStatus.OK),
             ({}, None, CallStatus.TIMEOUT))
    aborted = code.run(world.runtime, _context(world))
    assert aborted.status is Status.FAILED and aborted.facts["blockers"][0]["category"] == PLAN_GAP
    assert aborted.summary == "实施中止，方案走不通：recent 被三处调用，要改公共实现"
    failed = code.run(world.runtime, _context(world))
    assert failed.status is Status.FAILED and failed.facts["blockers"][0]["category"] == LOCAL


def test_the_next_round_continues_the_session_with_the_local_problems(world: Any,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    world.clock.advance(timedelta(minutes=1))
    coded = world.handoff("implement.code", {"sessionId": "S-1", "conditions": []}, at=world.clock.now())
    blockers = [{"check": "review", "kind": "root-cause-unfixed", "location": "src/orders.py:2", "summary": "仍只取当天",
                 "category": LOCAL, "trigger": "days 为 0"}]
    reviewed = world.handoff(REVIEW, {"blockers": blockers}, Status.FAILED, at=world.clock.now())
    coded, reviewed = replace(coded, round=1), replace(reviewed, round=1)
    fake = _install(monkeypatch, world, (FIX, _output(), CallStatus.OK))
    result = code.run(world.runtime, _context(world, round=2, implement_code=coded, implement_review=reviewed))
    request = fake.requests[0]
    assert request.point == CONTINUE and request.resume_session == "S-1"
    assert request.variables["corrections"] == ("- [review/root-cause-unfixed] src/orders.py:2：仍只取当天"
                                                "(触发条件：days 为 0)")
    assert result.round == 2


def test_a_change_of_conditions_starts_a_new_session(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """上一轮按高风险选的模型，这一轮不再高风险：工具与模型可能变了，不续接会话。"""
    world.clock.advance(timedelta(minutes=1))
    coded = replace(world.handoff("implement.code", {"sessionId": "S-1", "conditions": [HIGH_RISK]},
                                  at=world.clock.now()), round=1)
    blockers = [{"check": "review", "kind": "root-cause-unfixed", "location": "src/orders.py:2", "summary": "仍只取当天",
                 "category": LOCAL, "trigger": "days 为 0"}]
    reviewed = replace(world.handoff(REVIEW, {"blockers": blockers}, Status.FAILED, at=world.clock.now()), round=1)
    fake = _install(monkeypatch, world, (FIX, _output(), CallStatus.OK))
    code.run(world.runtime, _context(world, round=2, implement_code=coded, implement_review=reviewed))
    request = fake.requests[0]
    assert request.point == code.POINT and request.resume_session is None and request.conditions == ()


def test_only_the_first_category_of_problems_goes_back_to_coding(world: Any) -> None:
    blockers = [{"check": "review", "kind": "gap", "location": None, "summary": "方案没覆盖分页", "category": PLAN_GAP},
                {"check": "review", "kind": "bug", "location": "src/orders.py:2", "summary": "边界", "category": LOCAL}]
    reviewed = world.handoff(REVIEW, {"blockers": blockers}, Status.FAILED)
    reviewed = replace(reviewed, round=1)
    assert code.corrections_for(world.context(round=2, latest={REVIEW: reviewed})) == []
    assert code.failing(world.context(round=2, latest={REVIEW: reviewed}), 1) is reviewed


def test_tests_passed_reads_the_test_commands_of_the_check(world: Any) -> None:
    assert code.tests_passed_in({"commands": [{"name": "test", "result": "passed"},
                                              {"name": "lint", "result": "failed"}]})
    assert not code.tests_passed_in({"commands": [{"name": "test", "result": "failed"}]})
    assert not code.tests_passed_in({"commands": [{"name": "lint", "result": "passed"}]})


def test_a_check_with_more_problems_rolls_back_to_the_checkpoint(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    world.repo.write(FIX)
    world.clock.advance(timedelta(minutes=1))
    good = world.handoff(CHECK, {"commands": [{"name": "test", "result": "passed"}], "blockers": []},
                         at=world.clock.now())
    good = replace(good, round=1)
    fake = _install(monkeypatch, world, ({"src/orders.py": "broken\n"}, _output(), CallStatus.OK),
                    ({}, _output(), CallStatus.OK))
    code.run(world.runtime, _context(world, round=2, implement_check=good))  # 第 1 轮测试通过：记检查点
    checkpoint = world.runtime.workspace.subject_dir("0018") / code.CHECKPOINT_DIR
    assert (checkpoint / "files" / "src/orders.py").read_text() == FIX["src/orders.py"]
    world.clock.advance(timedelta(minutes=1))
    bad = world.handoff(CHECK, {"commands": [{"name": "test", "result": "failed"}],
                                "blockers": [{"check": "project", "kind": "test", "location": None,
                                              "summary": "测试失败", "category": LOCAL}]}, Status.FAILED,
                        at=world.clock.now())
    bad = replace(bad, round=2)
    result = code.run(world.runtime, _context(world, round=3, implement_check=bad))
    assert result.facts["rolledBackTo"] == 1
    assert fake.requests[1].variables["corrections"].startswith("- 上一轮的改动已撤销，worktree 已恢复到第 1 轮的检查点")
    assert Path(world.repo.path / "src/orders.py").read_text() == FIX["src/orders.py"]
