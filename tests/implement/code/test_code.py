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
from tightrein.implement import implement
from tightrein.implement.check.findings import LOCAL, NEEDS_USER, PLAN_GAP
from tightrein.implement.code import code
from tightrein.implement.context import Decision
from tightrein.implement.prompts.code import CONTINUE
from tightrein.protocol.handoff import Status, load_schema, schema_errors, write
from tightrein.protocol.naming import FileName, format_iso
from tightrein.protocol.process import Interrupted
from tightrein.protocol.recovery import CHECKPOINT_COMMIT

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
    assert request.read_paths == () and request.variables["designs"] == "无"  # 没有项目设计文档
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
    designs = world.runtime.workspace.designs_dir
    designs.mkdir(parents=True)
    (designs / "orders.md").write_text("# 订单\n", encoding="utf-8")
    fake = _install(monkeypatch, world, (FIX, _output(), CallStatus.OK))
    result = code.run(world.runtime, _context(world, round=2, implement_code=coded, implement_review=reviewed))
    request = fake.requests[0]
    assert request.point == CONTINUE and request.resume_session == "S-1"
    assert request.read_paths == (designs,) and "`designs/orders.md`" in request.variables["designs"]
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


# 发布合并 main 冲突，退回编码解决

MAIN_ORDERS = "def recent(days):\n    return days + 1\n"
FIX_ORDERS = "def recent(days):\n    return days * 7\n"
BOTH_ORDERS = "def recent(days):\n    return days * 7 + 1\n"


def _returned(world: Any, main_side: dict[str, str | None], fix_side: dict[str, str | None]) -> tuple[Any, str]:
    """main 与修复分支(fix/18-orders)各提交一次；发布合并时冲突、放弃了合并，退回编码(release.pr 的 backTo，
    检查点为修复分支当时的 HEAD)。"""
    main = world.repo.commit("feat: one more day", main_side)
    world.repo.git("checkout", "-q", "-b", "fix/18-orders", world.repo.base)
    fixed = world.repo.commit("fix: week", fix_side)
    returned = world.handoff("release.pr", {"backTo": code.POINT, "main": main,
                                            "conflicts": sorted(set(main_side) & set(fix_side)),
                                            "mainCommits": [f"{main[:12]} feat: one more day"],
                                            CHECKPOINT_COMMIT: fixed})
    return replace(_context(world, round=2), returned=returned), main


def _conflicting(world: Any) -> tuple[Any, str]:
    """main 与修复分支都改了 src/orders.py 的同一行。"""
    return _returned(world, {"src/orders.py": MAIN_ORDERS}, {"src/orders.py": FIX_ORDERS})


def test_a_round_sent_back_for_conflicts_merges_main_and_commits_the_resolution(
        world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    context, main = _conflicting(world)
    fixed = world.repo.git("rev-parse", "HEAD").strip()
    fake = _install(monkeypatch, world, ({"src/orders.py": BOTH_ORDERS}, _output(), CallStatus.OK))
    result = code.run(world.runtime, context)
    assert result.status is Status.PASSED
    corrections = fake.requests[0].variables["corrections"]
    assert corrections.startswith("- 发布前合并最新的 origin/main") and "冲突标记：src/orders.py" in corrections
    assert "feat: one more day" in corrections and "保留两侧的意图" in corrections
    git = world.git()
    head = git.head().commit
    assert git.merge_head() is None and git.status().clean
    assert world.repo.git("log", "-1", "--format=%P").split() == [fixed, main]  # 程序完成的合并提交
    assert world.repo.git("log", "-1", "--format=%s").strip() == "Merge origin/main into fix/18-orders"
    assert result.facts["mergedMain"] == {"main": main, "conflicts": ["src/orders.py"], "unmergeable": [],
                                          "side": None, "commit": head}
    # 本轮改动相对合并进来的 main 算
    assert result.facts["baseCommit"] == main and result.facts["diffHash"] == git.diff_hash(main)
    assert [item["path"] for item in result.facts["changedFiles"]] == ["src/orders.py"]


def test_an_unresolved_conflict_drops_the_merge_for_the_next_round(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    context, _ = _conflicting(world)
    fixed = world.repo.git("rev-parse", "HEAD").strip()
    _install(monkeypatch, world, ({"tests/test_new.py": "def test_x():\n    pass\n"}, _output(), CallStatus.OK))
    result = code.run(world.runtime, context)
    assert result.status is Status.FAILED and result.facts["blockers"][0]["category"] == LOCAL
    assert "冲突没解决完" in result.summary
    git = world.git()
    # 放弃了合并，worktree 回到这一轮开始时(新文件也清掉)，下一轮重新合并
    assert git.merge_head() is None and git.status().clean and git.head().commit == fixed
    assert code.merge.pending(context, git) is not None


def test_mutually_exclusive_sides_go_to_the_user(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    context, _ = _conflicting(world)
    big = {"description": "两侧都改了 recent 的返回值：一侧按周、一侧加一天", "locations": ["src/orders.py:2"]}
    _install(monkeypatch, world, ({}, _output(status="aborted", bigIssue=big, release=None), CallStatus.OK))
    result = code.run(world.runtime, context)
    assert result.status is Status.FAILED and result.facts["blockers"][0]["category"] == NEEDS_USER
    assert result.summary.startswith("合并 main 的冲突无法兼顾两侧") and world.git().merge_head() is None


def test_after_the_merge_later_rounds_do_not_merge_again(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    context, main = _conflicting(world)
    world.repo.git("merge", "-q", "-s", "ours", "--no-edit", main)
    assert code.merge.pending(context, world.git()) is None
    fake = _install(monkeypatch, world, ({}, _output(), CallStatus.OK))
    code.run(world.runtime, context)
    assert not fake.requests[0].variables["corrections"].startswith("- 发布前合并")


BASE_USERS = "NAME = 'a'\n"
USERS_MAIN = "NAME = 'main'\n"
AGENT_EDIT = {"src/users.py": "NAME = 'agent'\n"}  # 改了 main 一侧自动合并、已暂存的文件


@pytest.mark.parametrize(("edits", "output", "status"), [
    (AGENT_EDIT, _output(), CallStatus.OK),  # 冲突标记还在
    (AGENT_EDIT, None, CallStatus.TIMEOUT),  # 调用失败
    ({**AGENT_EDIT, ".env": "TOKEN=x\n"}, _output(), CallStatus.OK),  # 越界
], ids=["unresolved", "call-failed", "violation"])
def test_a_merge_is_dropped_even_after_cleanly_merged_files_were_edited(
        world: Any, monkeypatch: pytest.MonkeyPatch, edits: dict[str, str | None], output: dict[str, Any] | None,
        status: CallStatus) -> None:
    """agent 改了 main 一侧自动合并并已暂存的文件：`merge --abort` 会因 Entry not uptodate 失败。放弃合并照样成功，
    交接照常写出，worktree 连同分支回到合并前。"""
    context, _ = _returned(world, {"src/orders.py": MAIN_ORDERS, "src/users.py": USERS_MAIN},
                           {"src/orders.py": FIX_ORDERS})
    fixed = world.repo.git("rev-parse", "HEAD").strip()
    _install(monkeypatch, world, (edits, output, status))
    result = code.run(world.runtime, context)
    assert result.status is Status.FAILED
    assert "restore_failed" not in [item["kind"] for item in result.facts["blockers"]]
    git = world.git()
    assert git.merge_head() is None and git.status().clean and git.head().commit == fixed
    assert (world.repo.path / "src/users.py").read_text() == BASE_USERS


def test_conflicts_without_markers_go_to_the_user_and_the_chosen_side_is_merged(
        world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """main 删了修复改过的文件、两侧都新加了同名的二进制文件：没有冲突标记，改文件解决不了。不调用模型，放弃合并，
    停下请用户选保留哪一侧；选了 main 一侧之后程序把这些文件写成 main 的版本，冲突标记照常交给模型。"""
    context, main = _returned(world, {"src/orders.py": MAIN_ORDERS, "src/users.py": None, "assets/logo.bin": "\0main"},
                              {"src/orders.py": FIX_ORDERS, "src/users.py": "NAME = 'fix'\n",
                               "assets/logo.bin": "\0fix"})
    fixed = world.repo.git("rev-parse", "HEAD").strip()
    fake = _install(monkeypatch, world)
    stopped = code.run(world.runtime, context)
    assert fake.requests == [] and stopped.status is Status.FAILED  # 不调用模型
    assert [(item["location"], item["category"]) for item in stopped.facts["blockers"]] == [
        ("assets/logo.bin", NEEDS_USER), ("src/users.py", NEEDS_USER)]
    assert "src/users.py(修复改了这个文件，main 删了它(或改了名))" in stopped.summary
    merged = stopped.facts["mergedMain"]
    assert merged["conflicts"] == ["src/orders.py"] and merged["side"] is None and merged["commit"] is None
    assert [item["path"] for item in merged["unmergeable"]] == ["assets/logo.bin", "src/users.py"]
    gate = stopped.facts["gate"]
    assert "src/users.py：修复改了这个文件" in gate["decision"] and "--option 2" in gate["options"][1]
    git = world.git()
    assert git.merge_head() is None and git.status().clean and git.head().commit == fixed
    # 用户选 main 一侧(approve --option 2)
    stopped = replace(stopped, created_at=format_iso(world.clock.now()))
    world.clock.advance(timedelta(minutes=1))
    decision = Decision(code.POINT, "approve", 2, None, format_iso(world.clock.now()))
    context = replace(context, round=3, decisions=[decision], latest={**context.latest, code.POINT: stopped})
    fake = _install(monkeypatch, world, ({"src/orders.py": BOTH_ORDERS}, _output(), CallStatus.OK))
    result = code.run(world.runtime, context)
    assert result.status is Status.PASSED and result.facts["mergedMain"]["side"] == "main"
    corrections = fake.requests[0].variables["corrections"]
    assert "下面的文件里留有冲突标记：src/orders.py。" in corrections  # 只说真留了冲突标记的文件
    assert "已按用户的选择写成main 一侧的版本" in corrections and "src/users.py(修复改了这个文件" in corrections
    assert git.merge_head() is None and world.repo.git("log", "-1", "--format=%P").split() == [fixed, main]
    assert world.repo.git("ls-files", "src/users.py") == "" and not (world.repo.path / "src/users.py").exists()
    assert (world.repo.path / "assets/logo.bin").read_bytes() == b"\0main"


def test_an_interrupted_merge_round_goes_back_to_before_the_merge(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """程序完成合并提交之后、交接写好之前被中断：续跑退回发布记下的检查点时分支也退回合并前(只退文件会把合并
    进来的改动撤掉，还会以为已经合并过)，下一轮重新合并。"""
    context, main = _returned(world, {"src/users.py": USERS_MAIN}, {"src/orders.py": FIX_ORDERS})
    fixed = world.repo.git("rev-parse", "HEAD").strip()
    assert context.returned is not None
    write(world.runtime.workspace.step_file("0018", FileName("release.pr", "handoff", "json")), context.returned)
    git = world.git()

    def interrupted(*args: Any) -> CallResult:
        assert git.is_ancestor(main, "HEAD")  # 程序已完成合并提交
        raise Interrupted

    monkeypatch.setattr(code, "ask", interrupted)
    with pytest.raises(Interrupted):
        code.run(world.runtime, context)
    implement._rewind(world.runtime, context)
    assert git.head().commit == fixed and git.status().clean and code.merge.pending(context, git) is not None
    _install(monkeypatch, world, ({}, _output(), CallStatus.OK))
    result = code.run(world.runtime, context)
    head = git.head().commit
    assert result.status is Status.PASSED and git.is_ancestor(main, "HEAD")  # 不因记过的幂等键跳过这次合并
    assert result.facts["mergedMain"]["commit"] == head and head != fixed


def test_an_interrupted_round_with_conflicts_left_drops_the_merge(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """合并停在进行中、agent 改了一半时被中断：续跑放弃这次合并、文件回到检查点。"""
    context, _ = _returned(world, {"src/orders.py": MAIN_ORDERS, "src/users.py": USERS_MAIN},
                           {"src/orders.py": FIX_ORDERS})
    fixed = world.repo.git("rev-parse", "HEAD").strip()
    assert context.returned is not None
    write(world.runtime.workspace.step_file("0018", FileName("release.pr", "handoff", "json")), context.returned)

    def interrupted(*args: Any) -> CallResult:
        world.repo.write(AGENT_EDIT)
        raise Interrupted

    monkeypatch.setattr(code, "ask", interrupted)
    with pytest.raises(Interrupted):
        code.run(world.runtime, context)
    implement._rewind(world.runtime, context)
    git = world.git()
    assert git.merge_head() is None and git.status().clean and git.head().commit == fixed


def test_an_agents_own_commit_is_taken_back_with_the_round(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """agent 自己提交了(调用时的边界比对报越界)：撤回本轮时分支也退回这一轮开始时的提交。"""
    world.repo.git("checkout", "-q", "-b", "fix/18-orders")
    start = world.repo.git("rev-parse", "HEAD").strip()
    world.repo.write({"tests/test_week.py": "def test_week():\n    pass\n"})

    def committing(*args: Any) -> CallResult:
        world.repo.commit("fix: by the agent", FIX)
        return CallResult(CallStatus.BOUNDARY, "claude", "opus", violations=["HEAD 新建了提交"])

    monkeypatch.setattr(code, "ask", committing)
    result = code.run(world.runtime, _context(world))
    assert result.status is Status.FAILED and result.facts["violations"]
    git = world.git()
    assert git.head().commit == start and git.status().changed_paths == ("tests/test_week.py",)
    assert (world.repo.path / "src/orders.py").read_text() == "def recent(days):\n    return days\n"
