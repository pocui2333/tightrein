import json
from datetime import date

import pytest
from runner_samples import fixture, task
from runner_world import INVALID, NOW, SCHEMA, VALID, FakeRun, RunnerWorld, claude_lines

from tightrein.domain.enums import Access, AgentSessionStatus, RunnerStatus, Stage
from tightrein.runner import sessions
from tightrein.runner.result import RunnerConfigError, Usage
from tightrein.runner.task import Subject
from tightrein.runner.transcript import read
from tightrein.store.repos import agent_sessions, budget_usage

FIRST_INPUT = "Issue 文件：issues/0007-order-query-500.md"


@pytest.fixture
def make_world(repos, make_config, tmp_path):
    worlds = []

    def build(*runs, **config_changes):
        world = RunnerWorld(repos, make_config, tmp_path, *runs, **config_changes)
        worlds.append(world)
        return world

    yield build
    for world in worlds:
        world.conn.close()


def session_task(world, **changes):
    fields = {"stage": Stage.FIX, "role": "fix-session", "subject": Subject("issue", "0007"), "interactive": True,
              "output_schema": None, "access": Access.READ_ONLY, "allowed_commands": ("tightrein fix",)}
    return task(world.fix, **{**fields, **changes})


def claude_session(world, text=None):
    """模拟 Claude Code 在会话中写下的本机会话记录；会话 ID 取启动参数中核心生成的那个。"""
    def write(invocation):
        adapter = world.registry.adapter("claude")
        path = adapter.session_file(invocation.cwd, invocation.argv[2])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text or fixture("claude", "session.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
        world.repos.write(invocation.cwd, "src/OrderService.cs", "fix apply 写入的改动\n")
    return write


def test_a_new_claude_session(make_world):
    world = make_world()
    world.launcher.runs.append(FakeRun(action=claude_session(world)))
    result = world.runner().run_interactive(session_task(world), clock=world.clock, first_input=FIRST_INPUT)
    invocation = world.launcher.interactive[0]
    session_id = invocation.argv[2]
    assert invocation.argv[1] == "--session-id"
    assert invocation.argv[-2:] == ("--", FIRST_INPUT)
    assert (result.status, result.output, result.session_id, result.attempts) == (RunnerStatus.OK, {}, session_id, 1)
    assert result.usage == Usage(12200, 100, 6000, None, False)
    stored = agent_sessions.find(world.conn, stage=Stage.FIX, role="fix-session")
    assert [(item.session_id, item.status, item.ended_at) for item in stored] == [
        (session_id, AgentSessionStatus.CLOSED, NOW),
    ]
    events = read(world.layout.transcript("R-20261005-030000-triage", "fix-session", "0007"))
    assert [event["type"] for event in events][:3] == ["message", "message", "tool-call"]
    assert budget_usage.get(world.conn, Stage.FIX, date(2026, 10, 5)).input_tokens == 12200
    assert world.layout.readonly_guard("fix-0007").exists() is False


def test_the_finalize_call_collects_the_structured_result(make_world):
    world = make_world()
    world.launcher.runs += [FakeRun(action=claude_session(world)), FakeRun(claude_lines(VALID))]
    result = world.runner().run_interactive(session_task(world, output_schema=SCHEMA), clock=world.clock,
                                            first_input=FIRST_INPUT)
    assert (result.status, result.output, result.attempts) == (RunnerStatus.OK, VALID, 2)
    finalize = world.launcher.invocations[0]
    assert finalize.argv[1:4] == ("-p", "--resume", world.launcher.interactive[0].argv[2])
    assert result.usage.cost_usd == 0.5


def test_an_invalid_finalize_result(make_world):
    world = make_world()
    world.launcher.runs += [FakeRun(action=claude_session(world)), FakeRun(claude_lines(INVALID))]
    result = world.runner().run_interactive(session_task(world, output_schema=SCHEMA), clock=world.clock,
                                            first_input=FIRST_INPUT)
    assert (result.status, result.output) == (RunnerStatus.SCHEMA_INVALID, None)


def test_hidden_path_reads_in_a_session_are_violations(make_world):
    world = make_world()
    hidden = str(world.layout.regression_dir("0007") / "check.yaml")
    line = {"type": "assistant", "timestamp": "2026-10-05T03:00:09Z", "message": {"id": "m1", "content": [
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": hidden}}]}}
    world.launcher.runs.append(FakeRun(action=claude_session(world, json.dumps(line) + "\n")))
    result = world.runner().run_interactive(session_task(world), clock=world.clock, first_input=FIRST_INPUT)
    assert (result.status, result.error_type) == (RunnerStatus.GUARD_VIOLATION, "hidden-path-read")
    assert agent_sessions.find(world.conn)[0].status is AgentSessionStatus.CLOSED


def test_resume_the_latest_session(make_world):
    world = make_world(FakeRun())
    earlier = session_task(world)
    sessions.open_session(world.conn, earlier, "claude", NOW.replace(hour=1), "0d9f8e7c")
    result = world.runner().resume_interactive(session_task(world), clock=world.clock, first_input="继续")
    assert world.launcher.interactive[0].argv[1:3] == ("--resume", "0d9f8e7c")
    assert "--append-system-prompt-file" not in world.launcher.interactive[0].argv
    assert result.status is RunnerStatus.OK
    assert sessions.latest(world.conn, earlier).session_id == "0d9f8e7c"


def test_resume_without_a_session_is_unsupported(make_world):
    world = make_world()
    result = world.runner().resume_interactive(session_task(world), clock=world.clock, first_input="继续")
    assert (result.status, result.error_type, result.attempts) == (RunnerStatus.FAILED, "resume-unsupported", 0)
    assert world.launcher.interactive == []


def test_agy_sessions_are_refused_before_anything_is_recorded(make_world):
    world = make_world()
    with pytest.raises(RunnerConfigError, match="agy 不支持交互会话"):
        world.runner().run_interactive(session_task(world, tool="agy"), clock=world.clock, first_input=FIRST_INPUT)
    assert world.launcher.interactive == []
    assert agent_sessions.find(world.conn, stage=Stage.FIX, role="fix-session") == []


def test_codex_sessions_are_located_after_they_end(make_world):
    world = make_world(stages={"fix": {"tool": "codex", "session": {"tool": "codex"},
                                       "review": {"deep": {"tool": "claude"}}}})

    def codex_session(invocation):
        adapter = world.registry.adapter("codex")
        path = adapter.home / "sessions" / "2026" / "10" / "05" / "rollout-2026-10-05T03-00-02-x.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = fixture("codex", "rollout.jsonl").read_text(encoding="utf-8")
        path.write_text(text.replace("/ws/worktrees/fix-0007", str(invocation.cwd)), encoding="utf-8")

    world.launcher.runs.append(FakeRun(action=codex_session))
    result = world.runner().run_interactive(session_task(world), clock=world.clock, first_input=FIRST_INPUT)
    assert (result.status, result.session_id, result.tool) == (
        RunnerStatus.OK, "0199b000-1111-7222-8333-444455556666", "codex",
    )
    assert result.usage.input_tokens == 9000
    assert agent_sessions.find(world.conn)[0].session_id == "0199b000-1111-7222-8333-444455556666"


def test_interactive_runs_need_interactive_tasks(make_world):
    world = make_world()
    with pytest.raises(RunnerConfigError):
        world.runner().run_interactive(session_task(world, interactive=False, output_schema=SCHEMA),
                                       clock=world.clock, first_input="x")
