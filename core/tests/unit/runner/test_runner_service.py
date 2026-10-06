import json
import os
from datetime import date

import pytest
from runner_samples import task
from runner_world import INVALID, SCHEMA, SESSION, VALID, FakeRun, RunnerWorld, claude_lines

from tightrein.domain.enums import Access, RunnerStatus, Stage
from tightrein.guards.service import Guards
from tightrein.runner.adapters.replay import ReplayAdapter
from tightrein.runner.recording import RecordingSet, index_entry
from tightrein.runner.result import RunnerConfigError, Usage
from tightrein.runner.task import Instructions, Limits, SkillRef, Subject
from tightrein.runner.transcript import read
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import budget_usage
from tightrein.vcs.process import VcsProcess


def triage_task(world, **changes):
    instructions = Instructions("判断以下主张是否成立", (SkillRef("triage", ("roles/claim-verifier.md",)),))
    return task(world.readonly, **{"output_schema": SCHEMA, "instructions": instructions, **changes})


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
        for directory, _, _ in os.walk(world.readonly):
            os.chmod(directory, 0o755)


def test_a_successful_run(make_world):
    seen = {}

    def agent(invocation):
        seen["env"] = invocation.env
        with pytest.raises(PermissionError):
            (invocation.cwd / "README.md").write_text("x", encoding="utf-8")

    world = make_world(FakeRun(claude_lines(VALID, tools=[("Read", {"file_path": "src/OrderService.cs"})]),
                               action=agent))
    result = world.runner().run(triage_task(world), clock=world.clock)
    assert (result.status, result.output, result.attempts, result.session_id) == (RunnerStatus.OK, VALID, 1, SESSION)
    assert (result.tool, result.model) == ("claude", "claude-opus")
    assert result.usage == Usage(1000, 100, None, 0.5, False)
    assert result.transcript_path == "transcripts/claim-verifier-P-0042.jsonl"
    assert result.guard_report == "raw/guards/claim-verifier-P-0042.json"
    invocation = world.launcher.invocations[0]
    assert invocation.argv[0] == "/usr/local/bin/claude"
    assert ("--max-turns", "40") == invocation.argv[invocation.argv.index("--max-turns"):][:2]
    prompt = invocation.stdin.decode("utf-8")
    assert "分诊的做法。" in prompt and "取证角色说明" in prompt and "`" * 3 not in prompt
    assert "GH_TOKEN" not in seen["env"]
    assert seen["env"]["TIGHTREIN_WORKSPACE"] == str(world.layout.root)
    assert seen["env"]["TIGHTREIN_TRACE_ID"] == world.tracer.trace_id
    events = read(world.layout.transcript("R-20261005-030000-triage", "claim-verifier", "P-0042"))
    assert [event["type"] for event in events] == ["session-start", "tool-call", "usage", "result"]
    raw = world.layout.runner_raw_dir("R-20261005-030000-triage", "claim-verifier", "P-0042")
    assert json.loads((raw / "result.json").read_text(encoding="utf-8"))["status"] == "ok"
    assert len(json.loads((raw / "task.json").read_text(encoding="utf-8"))["taskSha256"]) == 64
    assert (raw / "stdout.jsonl").read_text(encoding="utf-8").count("\n") == 3
    assert budget_usage.get(world.conn, Stage.TRIAGE, date(2026, 10, 5)).cost_usd == 0.5
    span = world.spans()[0]
    assert (span.status, span.agent, span.cost_usd, span.attributes["attempts"]) == ("ok", "claude", 0.5, 1)


def test_limits_default_to_the_stage_settings(make_world):
    world = make_world(FakeRun(claude_lines(VALID)))
    world.runner().run(triage_task(world, limits=Limits()), clock=world.clock)
    argv = world.launcher.invocations[0].argv
    assert argv[argv.index("--max-turns") + 1] == "30"


def test_schema_invalid_output_is_retried_in_the_same_session(make_world):
    world = make_world(FakeRun(claude_lines(INVALID)), FakeRun(claude_lines(VALID)))
    result = world.runner().run(triage_task(world), clock=world.clock)
    assert (result.status, result.attempts, result.output) == (RunnerStatus.OK, 2, VALID)
    assert result.usage.cost_usd == 1.0
    retry = world.launcher.invocations[1]
    assert retry.argv[-2:] == ("--resume", SESSION)
    assert retry.stdin.decode("utf-8").startswith(f"上一次的输出不符合 {SCHEMA}")
    events = read(world.layout.transcript("R-20261005-030000-triage", "claim-verifier", "P-0042"))
    assert [event["attempt"] for event in events if event["type"] == "error"] == [1]
    assert {event["attempt"] for event in events} == {1, 2}


def test_output_invalid_twice_is_schema_invalid(make_world):
    world = make_world(FakeRun(claude_lines(INVALID)), FakeRun(claude_lines(INVALID)))
    result = world.runner().run(triage_task(world), clock=world.clock)
    assert (result.status, result.error_type, result.output, result.attempts) == (
        RunnerStatus.SCHEMA_INVALID, None, None, 2,
    )


def agy_lines(structured):
    return [json.dumps(line, ensure_ascii=False) for line in (
        {"event": "init", "conversation_id": "a1", "init": {"model": "gemini-3.1-pro-high"}},
        {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "agent_response",
                                                 "usage": {"input_tokens": 100, "output_tokens": 10}}},
        {"event": "result", "result": {"conversation_id": "a1", "status": "SUCCESS", "response": "",
                                       "structured_output": structured}},
    )]


def test_agy_format_retries_continue_the_conversation(make_world, make_config):
    models = {**make_config().data["models"],
              "gemini": {"tool": "agy", "model": "gemini-3.1-pro-high", "inputUsdPerMTok": 2, "outputUsdPerMTok": 12}}
    world = make_world(FakeRun(agy_lines(None)), FakeRun(agy_lines(VALID)), models=models,
                       routes={**make_config().data["routes"], "learn.rule-writer": "gemini"})
    result = world.runner().run(triage_task(world, stage=Stage.IMPROVE, route="learn.rule-writer"),
                                clock=world.clock)
    assert (result.status, result.tool, result.model, result.attempts, result.session_id) == (
        RunnerStatus.OK, "agy", "gemini-3.1-pro-high", 2, "a1")
    first, second = (invocation.argv for invocation in world.launcher.invocations)
    assert "--conversation" not in first and "--json-schema" in first
    assert second[second.index("--conversation") + 1] == "a1"
    assert "# 重试说明" not in first[-1] and second[-1].startswith("上一次的输出不符合")


def codex_lines(commands=0, input_tokens=200_000):
    lines = [{"type": "thread.started", "thread_id": "t1"}]
    for number in range(commands):
        lines.append({"type": "item.started", "item": {"id": f"c{number}", "type": "command_execution",
                                                       "command": "ls", "status": "in_progress"}})
    lines += [{"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": json.dumps(VALID)}},
              {"type": "turn.completed", "usage": {"input_tokens": input_tokens, "output_tokens": 10_000}}]
    return [json.dumps(line) for line in lines]


def fix_task(world, **changes):
    return task(world.fix, stage=Stage.FIX, role="fix-executor", output_schema=SCHEMA, access=Access.WORKSPACE_WRITE,
                subject=Subject("issue", "0007"), route="fix.executor", **changes)


def test_costs_are_estimated_when_the_tool_does_not_report_them(make_world):
    world = make_world(FakeRun(codex_lines()))
    result = world.runner().run(fix_task(world), clock=world.clock)
    assert (result.status, result.tool, result.model) == (RunnerStatus.OK, "codex", "gpt-5")
    assert result.usage.cost_usd == pytest.approx(0.35)
    assert result.usage.cost_estimated
    assert budget_usage.get(world.conn, Stage.FIX, date(2026, 10, 5)).estimated


def test_core_counts_turns_and_cost_for_tools_without_native_limits(make_world):
    world = make_world(FakeRun(codex_lines(commands=3)), FakeRun(codex_lines(input_tokens=2_000_000)))
    turns = world.runner().run(fix_task(world, limits=Limits(max_turns=2)), clock=world.clock)
    assert (turns.status, turns.error_type) == (RunnerStatus.LIMIT_REACHED, "turn-limit")
    costly = world.runner().run(fix_task(world, attempt=2, limits=Limits(max_cost_usd=1.0)), clock=world.clock)
    assert (costly.status, costly.error_type) == (RunnerStatus.LIMIT_REACHED, "cost-limit")


def test_timeouts_native_limits_and_tool_errors(make_world):
    world = make_world(FakeRun(timeout=True), FakeRun(claude_lines(VALID, subtype="error_max_turns"), exit_code=1),
                       FakeRun(["not json"], exit_code=2, stderr="Error: invalid API key"))
    runner = world.runner()
    timed_out = runner.run(triage_task(world), clock=world.clock)
    assert (timed_out.status, timed_out.error_type, timed_out.output) == (RunnerStatus.LIMIT_REACHED, "timeout", None)
    limited = runner.run(triage_task(world, attempt=2), clock=world.clock)
    assert (limited.status, limited.error_type) == (RunnerStatus.LIMIT_REACHED, "turn-limit")
    failed = runner.run(triage_task(world, attempt=3), clock=world.clock)
    assert (failed.status, failed.error_type, failed.attempts) == (RunnerStatus.FAILED, "tool-error", 1)
    events = read(world.layout.transcript("R-20261005-030000-triage", "claim-verifier", "P-0042"))
    assert "Error: invalid API key" in events[-1]["text"]
    assert "Error: invalid API key" in world.spans()[-1].reason


def test_transient_api_errors_retry_the_same_call(make_world):
    """临时的接口或网络错误就地重试同一次调用，不算格式重试；其他失败不重试。"""
    world = make_world(FakeRun(["not json"], exit_code=1, stderr='API error (attempt 1): request failed: Post "x": EOF'),
                       FakeRun(claude_lines(VALID)), FakeRun(["not json"], exit_code=2, stderr="Error: invalid API key"))
    runner = world.runner()
    recovered = runner.run(triage_task(world), clock=world.clock)
    assert (recovered.status, recovered.attempts) == (RunnerStatus.OK, 1) and len(world.launcher.invocations) == 2
    failed = runner.run(triage_task(world, attempt=2), clock=world.clock)
    assert failed.status is RunnerStatus.FAILED and len(world.launcher.invocations) == 3


def test_missing_tools_are_reported(make_world):
    world = make_world(FileNotFoundError(2, "No such file"))
    world.registry.which = lambda name: None
    result = world.runner().run(triage_task(world), clock=world.clock)
    assert (result.status, result.error_type) == (RunnerStatus.FAILED, "tool-unavailable")
    world.registry.which = lambda name: f"/usr/local/bin/{name}"
    result = world.runner().run(triage_task(world, attempt=2), clock=world.clock)
    assert (result.status, result.error_type) == (RunnerStatus.FAILED, "tool-unavailable")


def test_the_daily_budget_stops_the_stage(make_world):
    world = make_world()
    budget_usage.add(world.conn, Stage.TRIAGE, date(2026, 10, 5), 5.0, 1, 1, False, world.clock)
    result = world.runner().run(triage_task(world), clock=world.clock)
    assert (result.status, result.error_type, result.attempts, result.transcript_path) == (
        RunnerStatus.LIMIT_REACHED, "daily-budget", 0, None,
    )
    assert world.launcher.invocations == []


def test_guard_violations_stop_without_retry(make_world):
    def agent(invocation):
        os.chmod(invocation.cwd / "README.md", 0o644)
        (invocation.cwd / "README.md").write_text("changed\n", encoding="utf-8")

    world = make_world(FakeRun(claude_lines(INVALID), action=agent))
    result = world.runner().run(triage_task(world), clock=world.clock)
    assert (result.status, result.error_type, result.attempts) == (RunnerStatus.GUARD_VIOLATION, "readonly-modified", 1)
    assert [item.path for item in result.violations] == ["README.md"]
    assert len(world.launcher.invocations) == 1


def test_blocked_runs_do_not_start_the_agent(make_world):
    world = make_world()
    world.repos.write(world.readonly, ".env", "DB_PASSWORD=x\n")
    result = world.runner().run(triage_task(world), clock=world.clock)
    assert (result.status, result.error_type, result.attempts) == (
        RunnerStatus.GUARD_VIOLATION, "credential-present", 0,
    )
    assert result.guard_report == "raw/guards/claim-verifier-P-0042.json"
    assert world.launcher.invocations == []


def test_replay_runs_without_processes(make_world, tmp_path):
    world = make_world()
    replayed_task = triage_task(world)
    recording = tmp_path / "recordings" / "claim-verifier-P-0042.1"
    recording.mkdir(parents=True)
    usage = {"inputTokens": 10, "outputTokens": 5, "cachedInputTokens": None, "costUsd": 0.2, "costEstimated": False}
    (recording / "result.json").write_text(json.dumps({
        "status": "ok", "errorType": None, "output": VALID, "usage": usage, "durationMs": 1, "attempts": 1,
        "tool": "codex", "model": "gpt-5", "sessionId": "t1", "violations": [],
    }), encoding="utf-8")
    (tmp_path / "recordings" / "index.json").write_text(json.dumps({"recordings": [
        index_entry(replayed_task, recording.name)]}), encoding="utf-8")
    replay = ReplayAdapter(RecordingSet(tmp_path / "recordings"), VcsProcess(environ=world.repos.environ))
    result = world.runner(replay=replay).run(replayed_task, clock=world.clock, runner_override="replay")
    assert (result.status, result.output, result.tool, result.model) == (RunnerStatus.OK, VALID, "codex", "gpt-5")
    assert world.launcher.invocations == []
    assert budget_usage.get(world.conn, Stage.TRIAGE, date(2026, 10, 5)).cost_usd == 0.2
    raw = world.layout.runner_raw_dir("R-20261005-030000-triage", "claim-verifier", "P-0042")
    started = json.loads((raw / "started.json").read_text(encoding="utf-8"))
    assert (started["role"], started["subject"], started["tool"]) == ("claim-verifier", "P-0042", "replay")
    assert (raw / "result.json").stat().st_mtime >= (raw / "started.json").stat().st_mtime
    missing = world.runner(replay=replay).run(triage_task(world, attempt=2), clock=world.clock,
                                              runner_override="replay")
    assert (missing.status, missing.error_type) == (RunnerStatus.FAILED, "replay-missing")
    with pytest.raises(RunnerConfigError):
        world.runner().run(replayed_task, clock=world.clock, runner_override="replay")


def test_programming_errors_raise(make_world):
    world = make_world()
    with pytest.raises(RunnerConfigError):
        world.runner().run(triage_task(world, output_schema=None), clock=world.clock)
    with pytest.raises(RunnerConfigError):
        world.runner().run(triage_task(world, interactive=True), clock=world.clock)
    with pytest.raises(RunnerConfigError):
        world.runner().run(triage_task(world, stage=Stage.LEARN, route="learn.rule-writer"), clock=world.clock)


def test_the_output_mode_tells_agents_where_the_sandbox_is(make_world, tmp_path):
    seen = {}
    world = make_world(FakeRun(claude_lines(VALID), action=lambda invocation: seen.update(invocation.env)))
    world.layout = WorkspaceLayout(world.layout.root, tmp_path / "sandbox")
    world.guards = Guards(world.guards.git, world.guards.settings, world.layout, world.tool, tracer=world.tracer)
    world.runner().run(triage_task(world), clock=world.clock)
    assert (seen["TIGHTREIN_SANDBOX"], seen["TIGHTREIN_OUTPUT_DIR"]) == ("1", str(tmp_path / "sandbox"))
    assert (tmp_path / "sandbox" / "transcripts" / "claim-verifier-P-0042.jsonl").is_file()


def test_the_retry_note_shows_the_structured_output_when_the_tool_gives_one():
    from types import SimpleNamespace

    from tightrein.runner.service import raw_output

    assert raw_output(SimpleNamespace(final_text="", structured={"ok": "是"})) == '{"ok": "是"}'
    assert raw_output(SimpleNamespace(final_text="文本", structured=None)) == "文本"
