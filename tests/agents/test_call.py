import json
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tightrein.agents import call as agents_call
from tightrein.agents.call import (
    OVERFLOW_NOTE,
    OVERFLOW_REASON,
    TRUNCATED,
    AgentContext,
    call,
    checked_output,
    extract_json,
    params_for,
    raw_output,
    retry_note,
    truncate,
)
from tightrein.agents.params import Access, CallParams
from tightrein.agents.result import CallStatus
from tightrein.agents.tools import CallConfigError, Parsed
from tightrein.agents.tools.agy import AgyAdapter
from tightrein.agents.tools.claude import ClaudeAdapter
from tightrein.agents.tools.codex import CodexAdapter
from tightrein.agents.tools.replay import ReplayAdapter, index_entry
from tightrein.protocol import security
from tightrein.protocol.handoff import Tokens
from tightrein.protocol.limits import Breaker
from tightrein.protocol.naming import FileName, FixedClock
from tightrein.protocol.process import OVERFLOW, OVERFLOW_HINT, Command, Outcome, SubprocessRunner
from tightrein.protocol.resources import IssueBudget, Quota
from tightrein.protocol.security import Redactor, TreeSnapshot
from tightrein.settings.load import Settings
from tightrein.store.db import open_database
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout

NOW = datetime(2026, 10, 7, 5, 0, tzinfo=UTC)
RUN = "R-20261007T050000Z-assess"
SESSION = "5f0c2a1e-8d3b-4c6e-9a41-7b2d9e0f1c3a"
SCHEMA = {"type": "object", "properties": {"sameRootCause": {"type": "boolean"}, "reason": {"type": "string"}},
          "required": ["sameRootCause", "reason"]}
VALID = {"sameRootCause": False, "reason": "根因位置不同"}
INVALID = {"sameRootCause": "不是布尔值"}
MODELS = {"gpt": {"tool": "codex", "model": "gpt-5", "effort": None, "price": {"input": 1.25, "output": 10}}}
FENCE = "`" * 3


def claude_lines(structured: Any, *, subtype: str = "success", tools: tuple = ()) -> list[str]:
    lines: list[dict] = [{"type": "system", "subtype": "init", "session_id": SESSION, "model": "claude-opus"}]
    for number, (name, arguments) in enumerate(tools, start=1):
        lines.append({"type": "assistant", "session_id": SESSION, "message": {"id": f"m{number}", "content": [
            {"type": "tool_use", "id": f"toolu_{number}", "name": name, "input": arguments}]}})
    lines.append({"type": "result", "subtype": subtype, "is_error": subtype != "success", "num_turns": len(tools) + 1,
                  "result": json.dumps(structured, ensure_ascii=False), "session_id": SESSION,
                  "total_cost_usd": 0.5, "usage": {"input_tokens": 1000, "output_tokens": 100},
                  "structured_output": structured})
    return [json.dumps(line, ensure_ascii=False) for line in lines]


def claude_error(text: str) -> list[str]:
    return [json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": text,
                        "session_id": SESSION, "usage": {"input_tokens": 10, "output_tokens": 1}})]


def agy_lines(structured: Any) -> list[str]:
    return [json.dumps(line, ensure_ascii=False) for line in (
        {"event": "init", "conversation_id": "a1", "init": {"model": "gemini-3.8-flash-high"}},
        {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "agent_response",
                                                 "usage": {"input_tokens": 100, "output_tokens": 10}}},
        {"event": "result", "result": {"conversation_id": "a1", "status": "SUCCESS", "response": "",
                                       "structured_output": structured}},
    )]


def codex_lines(commands: tuple[str, ...] = (), input_tokens: int = 200_000, output_tokens: int = 10_000) -> list[str]:
    lines: list[dict] = [{"type": "thread.started", "thread_id": "t1"}]
    for number, command in enumerate(commands):
        lines.append({"type": "item.started", "item": {"id": f"c{number}", "type": "command_execution",
                                                       "command": f"bash -lc '{command}'", "status": "in_progress"}})
    lines += [{"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": json.dumps(VALID)}},
              {"type": "turn.completed", "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}]
    return [json.dumps(line, ensure_ascii=False) for line in lines]


@dataclass
class FakeRun:
    """一次录制的进程运行；action 在输出之前执行，模拟 agent 的行为(或在调用进行中检查状态)。"""

    lines: list[str] = field(default_factory=list)
    exit_code: int = 0
    stderr: str = ""
    timeout: bool = False
    start_error: str | None = None
    action: Callable[[Command], None] | None = None
    stopped: str | None = None  # 进程层面的终止原因(如 overflow)，输出照常给出


class FakeRunner:
    def __init__(self, *runs: FakeRun) -> None:
        self.runs = list(runs)
        self.commands: list[Command] = []

    def run(self, command: Command) -> Outcome:
        self.commands.append(command)
        run = self.runs.pop(0)
        if run.start_error is not None:
            return Outcome(None, "", "", 0, None, run.start_error)
        if run.action is not None:
            run.action(command)
        stopped = "timeout" if run.timeout else None
        seen: list[str] = []
        for line in run.lines:
            if stopped is not None:
                break
            seen.append(line)
            stopped = command.on_line(line) if command.on_line is not None else None
        stopped = stopped or run.stopped
        return Outcome(None if stopped else run.exit_code, "\n".join(seen), run.stderr, 10, stopped, None)


class Events:
    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, **event: Any) -> None:
        self.emitted.append(event)

    def summaries(self, kind: str) -> list[str]:
        return [event["summary"] for event in self.emitted if event["kind"] == kind]


class World:
    def __init__(self, tmp_path: Path, runs: tuple[FakeRun, ...], overrides: dict[str, Any]) -> None:
        self.clock = FixedClock(NOW)
        defaults = json.loads(ToolLayout.discover().defaults.read_text(encoding="utf-8"))
        self.settings = Settings.from_data(defaults, {"models": MODELS, **overrides})
        self.layout = WorkspaceLayout(tmp_path / "workspaces" / "demo")
        self.conn = open_database(self.layout.database, clock=self.clock)
        self.bin = tmp_path / "bin"
        self.bin.mkdir(parents=True)
        for tool in ("claude", "agy", "codex"):
            executable = self.bin / tool
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
        self.workdir = tmp_path / "work"
        self.workdir.mkdir()
        self.runner = FakeRunner(*runs)
        self.events = Events()
        self.sleeps: list[float] = []
        self.redactor = Redactor()
        environ = {"PATH": str(self.bin), "HOME": str(tmp_path), "ANTHROPIC_API_KEY": "sk-ant-api03-abcdefghijkl",
                   "GH_TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz0123"}
        self.context = AgentContext(
            settings=self.settings, layout=self.layout, conn=self.conn, clock=self.clock, runner=self.runner,
            redactor=self.redactor, events=self.events, environ=environ,  # type: ignore[arg-type]
            breaker=Breaker(self.conn, self.clock, self.settings), quota=Quota(self.conn, self.clock, self.settings),
            budget=IssueBudget(self.conn, self.clock, self.settings),
            adapters={"claude": ClaudeAdapter(), "agy": AgyAdapter(tmp_path / "agy-settings.json"),
                      "codex": CodexAdapter()},
            sleep=self.sleeps.append, random=lambda: 0.5, tool=ToolLayout(tmp_path / "tool"),
        )

    def params(self, point: str = "assess.triage", **changes: Any) -> CallParams:
        base = params_for(point, settings=self.settings, run=RUN, subject="P-0042", prompt="判断以下主张是否成立",
                          schema=SCHEMA, workdir=self.workdir)
        return replace(base, **changes)

    def codex(self, **changes: Any) -> CallParams:
        return self.params("implement.code", subject="0018", model=self.settings.model("gpt"), fallback=None,
                           **changes)

    def file(self, content: str, extension: str) -> Path:
        return self.layout.step_file("P-0042", FileName("assess.triage", content, extension))


@pytest.fixture
def make_world(tmp_path: Path) -> Iterator[Callable[..., World]]:
    worlds: list[World] = []

    def build(*runs: FakeRun, **overrides: Any) -> World:
        world = World(tmp_path / f"world{len(worlds)}", runs, overrides)
        worlds.append(world)
        return world

    yield build
    for world in worlds:
        world.conn.close()


# 成功与记录


def test_a_successful_call(make_world):
    world = make_world(FakeRun(claude_lines(VALID, tools=(("Read", {"file_path": "src/a.py"}),))))
    result = call(world.params(), world.context)
    assert (result.status, result.output, result.attempts, result.retries, result.session_id) == (
        CallStatus.OK, VALID, 1, 0, SESSION)
    assert (result.tool, result.model, result.cost_usd, result.cost_estimated) == ("claude", "opus", 0.5, False)
    assert result.tokens == Tokens(input=1000, output=100)
    command = world.runner.commands[0]
    assert command.argv[0] == str(world.bin / "claude")
    assert command.argv[command.argv.index("--max-turns") + 1] == "15"
    assert (command.timeout_s, command.idle_s) == (1800, 300)  # assess.triage 继承 assess 的 30m
    assert command.stdin == "判断以下主张是否成立"
    assert "ANTHROPIC_API_KEY" not in command.env and "GH_TOKEN" not in command.env
    assert command.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16000"
    started = json.loads(world.file("started", "json").read_text(encoding="utf-8"))
    assert (started["tool"], started["model"], started["effort"], started["status"]) == ("claude", "opus", "high", "ok")
    assert started["startedAt"] == started["endedAt"] == "2026-10-07T05:00:00Z"
    assert result.raw_path is None and not world.file("raw", "jsonl").exists()
    assert not world.file("prompt", "md").exists()
    assert world.context.budget.used("P-0042") == 1100
    # 被去掉的环境变量只记名字、不记值
    assert world.events.summaries("action") == ["子进程环境去掉了：ANTHROPIC_API_KEY、GH_TOKEN", "claude/opus：ok"]
    assert not any("sk-ant" in summary or "ghp_" in summary for summary in world.events.summaries("action"))


def test_the_started_marker_shows_a_call_in_progress(make_world):
    seen: dict[str, Any] = {}
    world = make_world()

    def peek(command: Command) -> None:
        seen.update(json.loads(world.file("started", "json").read_text(encoding="utf-8")))

    world.runner.runs.append(FakeRun(claude_lines(VALID), action=peek))
    call(world.params(), world.context)
    assert (seen["startedAt"], seen["endedAt"], seen["status"]) == ("2026-10-07T05:00:00Z", None, None)


def test_params_follow_the_call_point_settings(make_world):
    world = make_world()
    design = params_for("implement.design", settings=world.settings, run=RUN, subject="0018", prompt="p",
                        schema=None, workdir=world.workdir, conditions=("high_risk",), allowed_commands=("make test",),
                        round=2)
    assert (design.model.alias, design.fallback.alias, design.access, design.round) == (
        "fable", "sonnet", Access.READ, 2)
    assert design.allowed_commands[0] == "git grep" and design.allowed_commands[-1] == "make test"
    assert (design.limits.timeout_s, design.limits.turns) == (600, 15)
    code = params_for("implement.code", settings=world.settings, run=RUN, subject="0018", prompt="p", schema=None,
                      workdir=world.workdir)
    assert (code.access, code.limits.turns, code.fallback.alias) == (Access.WRITE, 30, "sonnet")
    refute = params_for("assess.refute", settings=world.settings, run=RUN, subject=None, prompt="p", schema=None,
                        workdir=world.workdir)
    assert (refute.model.tool, refute.fallback.alias, refute.network) == ("agy", "opus-mid", False)


# 格式不符


def test_schema_invalid_output_is_retried_in_the_same_session(make_world):
    world = make_world(FakeRun(claude_lines(INVALID)), FakeRun(claude_lines(VALID)))
    result = call(world.params(), world.context)
    assert (result.status, result.attempts, result.output, result.cost_usd) == (CallStatus.OK, 2, VALID, 1.0)
    retry = world.runner.commands[1]
    assert retry.argv[-2:] == ("--resume", SESSION)
    note = retry.stdin
    assert note.startswith("上一次的输出不符合要求的 schema")
    assert "- /sameRootCause：" in note and "'reason' is a required property" in note
    assert note.endswith(json.dumps(INVALID, ensure_ascii=False))  # 工具给了 structured，原输出取它而不是空文本


def test_output_invalid_twice_is_schema_invalid_and_saved(make_world):
    world = make_world(FakeRun(claude_lines(INVALID)), FakeRun(claude_lines(INVALID)))
    result = call(world.params(), world.context)
    assert (result.status, result.output, result.attempts) == (CallStatus.SCHEMA_INVALID, None, 2)
    assert "/sameRootCause" in result.error
    assert result.raw_path == world.file("raw", "jsonl")
    lines = result.raw_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["tightrein"]["attempt"] for line in lines if '"tightrein"' in line] == [1, 2]
    assert world.file("prompt", "md").read_text(encoding="utf-8").strip() == "判断以下主张是否成立"


def test_agy_format_retries_continue_the_conversation(make_world):
    world = make_world(FakeRun(agy_lines(None)), FakeRun(agy_lines(VALID)))
    result = call(world.params(model=world.settings.model("flash-high"), fallback=None), world.context)
    assert (result.status, result.tool, result.model, result.attempts, result.session_id) == (
        CallStatus.OK, "agy", "gemini-3.8-flash-high", 2, "a1")
    first, second = (command.argv for command in world.runner.commands)
    assert "--conversation" not in first and "--json-schema" in first
    assert second[second.index("--conversation") + 1] == "a1"
    assert second[-1].startswith("上一次的输出不符合") and second[-1].endswith("(空)")
    assert result.cost_estimated and result.cost_usd == pytest.approx(200 * 0.5 / 1e6 + 20 * 3 / 1e6)


def test_without_a_session_the_note_is_appended_to_a_new_call(make_world):
    no_session = [line for line in claude_lines(INVALID) if '"init"' not in line]
    no_session[-1] = no_session[-1].replace(f'"session_id": "{SESSION}"', '"session_id": null')
    world = make_world(FakeRun(no_session), FakeRun(claude_lines(VALID)))
    result = call(world.params(), world.context)
    assert result.status is CallStatus.OK
    retry = world.runner.commands[1]
    assert "--resume" not in retry.argv
    assert retry.stdin.startswith("判断以下主张是否成立\n\n上一次的输出不符合")


def test_output_over_the_limit_is_a_protocol_error_that_resumes_the_session(make_world):
    """标准输出超限被终止：归为格式不符(schema_invalid)，续接同一会话并提示大结果写文件、以路径引用。"""
    world = make_world(FakeRun(claude_lines(VALID), stderr=OVERFLOW_HINT.format(limit=64), stopped=OVERFLOW),
                       FakeRun(claude_lines(VALID)))
    result = call(world.params(), world.context)
    assert (result.status, result.output, result.attempts) == (CallStatus.OK, VALID, 2)
    retry = world.runner.commands[1]
    assert retry.argv[-2:] == ("--resume", SESSION)
    assert retry.stdin == OVERFLOW_NOTE.format(hint=OVERFLOW_REASON)
    assert "写到文件" in retry.stdin and "以路径引用" in retry.stdin


def test_output_over_the_limit_twice_is_schema_invalid(make_world):
    world = make_world(FakeRun(claude_lines(VALID), stopped=OVERFLOW), FakeRun(claude_lines(VALID), stopped=OVERFLOW))
    result = call(world.params(), world.context)
    assert (result.status, result.error, result.attempts) == (CallStatus.SCHEMA_INVALID, OVERFLOW_REASON, 2)


def test_removed_environment_variables_are_recorded_once_per_call(make_world):
    world = make_world(FakeRun(claude_lines(INVALID)), FakeRun(claude_lines(VALID)))
    assert call(world.params(), world.context).attempts == 2
    assert world.events.summaries("action").count("子进程环境去掉了：ANTHROPIC_API_KEY、GH_TOKEN") == 1


# 临时错误、认证失败、上限


def test_transient_api_errors_retry_the_same_call(make_world):
    stderr = 'API error (attempt 1): request failed: Post "x": EOF'
    world = make_world(FakeRun(["not json"], exit_code=1, stderr=stderr), FakeRun(claude_lines(VALID)))
    result = call(world.params(), world.context)
    assert (result.status, result.attempts) == (CallStatus.OK, 2)
    first, second = world.runner.commands
    assert (first.argv, first.stdin) == (second.argv, second.stdin)  # 同一次调用，不是续接
    assert world.sleeps == [0.5]


def test_transient_retries_stop_after_the_limit(make_world):
    overloaded = FakeRun(["not json"], exit_code=1, stderr="529 overloaded_error")
    world = make_world(overloaded, replace(overloaded), replace(overloaded))
    result = call(world.params(), world.context)
    assert (result.status, result.attempts) == (CallStatus.TRANSIENT, 3)
    assert world.sleeps == [0.5, 1.0]
    assert "overloaded_error" in result.error and result.raw_path is not None


def test_retry_after_is_followed(make_world):
    world = make_world(FakeRun(["x"], exit_code=1, stderr="rate limit exceeded, retry-after: 7"),
                       FakeRun(claude_lines(VALID)))
    assert call(world.params(), world.context).ok
    assert world.sleeps == [7.0]


def test_authentication_failures_stop_and_trip_the_breaker(make_world):
    world = make_world(FakeRun(["not json"], exit_code=2, stderr="Invalid API key · Please run /login"))
    result = call(world.params(), world.context)
    assert (result.status, result.attempts) == (CallStatus.AUTH_FAILED, 1)
    assert len(world.runner.commands) == 1
    assert not world.context.breaker.allow("claude")


def test_timeouts_native_limits_and_tool_errors_are_not_retried(make_world):
    world = make_world(FakeRun(claude_lines(VALID), timeout=True),
                       FakeRun(claude_lines(VALID, subtype="error_max_turns"), exit_code=1),
                       FakeRun(["not json"], exit_code=2, stderr="boom"))
    timed_out = call(world.params(), world.context)
    assert (timed_out.status, timed_out.output, timed_out.error) == (CallStatus.TIMEOUT, None, "进程被终止：timeout")
    assert call(world.params(subject="P-0043"), world.context).status is CallStatus.TURN_LIMIT
    failed = call(world.params(subject="P-0044"), world.context)
    assert failed.status is CallStatus.FAILED and "退出码 2" in failed.error and "boom" in failed.error
    assert len(world.runner.commands) == 3


def test_core_counts_turns_for_tools_without_native_limits(make_world):
    world = make_world(FakeRun(codex_lines(commands=("git log", "git show HEAD", "cat a.py"))))
    params = world.codex()
    result = call(replace(params, limits=replace(params.limits, turns=2)), world.context)
    assert (result.status, result.error) == (CallStatus.TURN_LIMIT, "工具调用超过 2 次")
    assert "--max-turns" not in world.runner.commands[0].argv


def test_commands_outside_the_allow_list_are_a_boundary_violation(make_world):
    world = make_world(FakeRun(codex_lines(commands=("git log", "rm -rf src"))))
    result = call(world.codex(), world.context)
    assert (result.status, result.attempts) == (CallStatus.BOUNDARY, 1)
    assert result.violations == ["command：不在白名单的命令 rm -rf src"]


def test_single_outputs_over_the_cap_are_stopped(make_world):
    world = make_world(FakeRun(codex_lines(output_tokens=20_000)))
    result = call(world.codex(), world.context)
    assert result.status is CallStatus.BUDGET_LIMIT and "超过上限 16000" in result.error


def test_costs_are_estimated_when_the_tool_does_not_report_them(make_world):
    world = make_world(FakeRun(codex_lines()))
    result = call(world.codex(), world.context)
    assert (result.status, result.tool, result.model) == (CallStatus.OK, "codex", "gpt-5")
    assert result.cost_usd == pytest.approx(0.35) and result.cost_estimated


# 每个 Issue 的用量上限与订阅额度


def test_the_issue_budget_stops_a_running_call_and_later_calls(make_world):
    world = make_world(FakeRun(codex_lines(input_tokens=10_000, output_tokens=100)))
    world.context.budget.add("0018", Tokens(input=1_995_000))
    running = call(world.codex(), world.context)
    assert running.status is CallStatus.BUDGET_LIMIT and "上限" in running.error
    assert world.context.budget.used("0018") == 1_995_000 + 10_100
    later = call(world.codex(), world.context)
    assert (later.status, later.attempts) == (CallStatus.BUDGET_LIMIT, 0)
    assert len(world.runner.commands) == 1


def test_a_rejected_rate_limit_event_halts_everything(make_world):
    rejected = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": 1791356400, "rateLimitType": "five_hour"}})
    lines = claude_lines(VALID)
    world = make_world(FakeRun([lines[0], rejected, *lines[1:]]))
    result = call(world.params(), world.context)
    assert (result.status, result.attempts) == (CallStatus.QUOTA_EXHAUSTED, 1)
    assert result.rate_limits[0].status == "rejected"
    assert world.context.quota.halted_until("claude") == datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
    assert any("全部停下" in summary for summary in world.events.summaries("decision"))
    later = call(world.params(subject="P-0043"), world.context)
    assert (later.status, later.attempts) == (CallStatus.QUOTA_EXHAUSTED, 0) and "重置" in later.error
    assert len(world.runner.commands) == 1  # 不换到另一个工具


def test_session_limit_messages_record_the_reset_time(make_world):
    world = make_world(FakeRun(claude_error("You've hit your session limit · resets 3:45pm (Asia/Tokyo)"), exit_code=1))
    assert call(world.params(), world.context).status is CallStatus.QUOTA_EXHAUSTED
    assert world.context.quota.halted_until("claude") == datetime(2026, 10, 7, 6, 45, tzinfo=UTC)


# 备用模型与依赖熔断


def test_refused_calls_switch_to_the_fallback_once(make_world):
    refusal = FakeRun(claude_error("Claude Code is unable to respond to this request, which appears to violate "
                                   "our Usage Policy"), exit_code=1)
    world = make_world(refusal, FakeRun(claude_lines(VALID)))
    result = call(world.params(), world.context)
    assert (result.status, result.model, result.attempts) == (CallStatus.OK, "sonnet", 2)
    second = world.runner.commands[1].argv
    assert second[second.index("--model") + 1] == "sonnet" and "--resume" not in second
    assert any("换备用模型 sonnet" in summary for summary in world.events.summaries("decision"))
    twice = make_world(replace(refusal), replace(refusal))
    assert call(twice.params(), twice.context).status is CallStatus.REFUSED
    assert len(twice.runner.commands) == 2


def test_a_missing_tool_switches_to_the_fallback(make_world, tmp_path):
    world = make_world(FakeRun(claude_lines(VALID)), tools={"agy": {"path": str(tmp_path / "missing-agy")}})
    result = call(world.params(model=world.settings.model("flash"), fallback=world.settings.model("sonnet")),
                  world.context)
    assert (result.status, result.tool, result.model, result.attempts) == (CallStatus.OK, "claude", "sonnet", 2)
    assert len(world.runner.commands) == 1  # 配置的路径不存在即不可用，不悄悄改用 PATH 中的 agy


def test_tools_that_cannot_start_are_unavailable(make_world):
    world = make_world(FakeRun(start_error="FileNotFoundError: claude"))
    result = call(world.params(fallback=None), world.context)
    assert (result.status, result.error) == (CallStatus.UNAVAILABLE, "FileNotFoundError: claude")


def test_the_dependency_breaker_routes_calls_to_the_fallback(make_world):
    world = make_world(FakeRun(claude_lines(VALID)))
    world.context.breaker.trip("agy")
    params = world.params(model=world.settings.model("flash-high"), fallback=world.settings.model("opus-mid"))
    result = call(params, world.context)
    assert (result.status, result.tool) == (CallStatus.OK, "claude")
    argv = world.runner.commands[0].argv
    assert argv[argv.index("--effort") + 1] == "medium"
    assert any("依赖熔断中" in summary for summary in world.events.summaries("decision"))
    blocked = call(replace(params, fallback=None), world.context)
    assert (blocked.status, blocked.attempts) == (CallStatus.UNAVAILABLE, 0)


# 边界、补要结构化结果、回放、配置错误


def test_boundary_violations_stop_without_retry(make_world, monkeypatch):
    world = make_world(FakeRun(claude_lines(INVALID)))
    (world.workdir / ".git").mkdir()
    taken: list[Path] = []

    def snapshot(repo: Path, runner: object) -> TreeSnapshot:
        taken.append(repo)
        return TreeSnapshot("", "before") if len(taken) == 1 else TreeSnapshot(" M README.md", "after")

    monkeypatch.setattr(agents_call, "snapshot", snapshot)
    monkeypatch.setattr(agents_call, "credentials_present", lambda repo, runner, patterns: [])
    result = call(world.params(), world.context)
    assert (result.status, result.attempts, result.output) == (CallStatus.BOUNDARY, 1, None)
    assert result.violations == [f"read_only_changed：{world.workdir} 工作树的内容有变化"]
    assert len(world.runner.commands) == 1 and taken == [world.workdir, world.workdir]


def test_uncommitted_changes_left_by_an_earlier_round_are_not_a_boundary_violation(make_world, monkeypatch):
    """边界按调用前后的差异比较：上一轮留在 worktree 里的未提交改动不算越界，这一次又改了才算。"""
    def git(*args: str) -> None:
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false",
                        "-c", "core.hooksPath=/dev/null", *args], cwd=world.workdir, check=True, capture_output=True)

    def touch(command: Command) -> None:
        (world.workdir / "a.txt").write_text("这一次改的\n", encoding="utf-8")

    world = make_world(FakeRun(claude_lines(VALID)), FakeRun(claude_lines(VALID), action=touch))
    git("init", "-q")
    (world.workdir / "a.txt").write_text("one\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-q", "-m", "init")
    (world.workdir / "a.txt").write_text("上一轮留下的\n", encoding="utf-8")
    (world.workdir / "left.txt").write_text("x\n", encoding="utf-8")
    real = SubprocessRunner()
    monkeypatch.setattr(agents_call, "snapshot", lambda repo, runner: security.snapshot(repo, real))
    monkeypatch.setattr(agents_call, "compare",
                        lambda before, after, repo, runner: security.compare(before, after, repo, real))
    monkeypatch.setattr(agents_call, "credentials_present", lambda repo, runner, patterns: [])
    assert call(world.params(), world.context).status is CallStatus.OK
    changed = call(world.params(subject="P-0043"), world.context)
    assert changed.status is CallStatus.BOUNDARY
    assert changed.violations == [f"read_only_changed：{world.workdir} 工作树的内容有变化"]


def test_credentials_in_the_worktree_stop_the_call_before_it_starts(make_world, monkeypatch):
    world = make_world(FakeRun(claude_lines(VALID)))
    (world.workdir / ".git").mkdir()
    checked: list[tuple[Path, tuple[str, ...]]] = []

    def present(repo: Path, runner: object, patterns: tuple[str, ...]) -> list[str]:
        checked.append((repo, patterns))
        return ["worktree 中有未跟踪的凭据文件 .env(匹配 .env)，删除或移出 worktree 后重试"]

    monkeypatch.setattr(agents_call, "credentials_present", present)
    result = call(world.params(), world.context)
    assert (result.status, result.attempts, world.runner.commands) == (CallStatus.BOUNDARY, 0, [])
    assert result.violations == ["credential：worktree 中有未跟踪的凭据文件 .env(匹配 .env)，删除或移出 worktree 后重试"]
    assert checked == [(world.workdir, tuple(world.settings.get("boundaries.protected.forbidden")))]


def test_reading_hidden_directories_or_credential_files_is_a_boundary_violation(make_world):
    key = Path("~/.ssh/id_ed25519")
    world = make_world(FakeRun(claude_lines(VALID, tools=(("Read", {"file_path": str(key)}),))))
    result = call(world.params(), world.context)
    assert (result.status, result.attempts) == (CallStatus.BOUNDARY, 1)
    assert result.violations == [f"hidden_read：Read 访问了隐藏目录 {key}"]
    # 每个 world 有自己的工作区，data/ 路径须取自发起调用的那个 world
    world = make_world()
    data = world.layout.data_dir / "issues" / "0018" / "handoff.json"
    world.runner.runs.append(FakeRun(claude_lines(VALID, tools=(("Grep", {"pattern": "x", "path": str(data)}),))))
    assert call(world.params(), world.context).violations == [f"hidden_read：Grep 访问了隐藏目录 {data}"]
    # 额外可读目录放行；凭据文件名只有确实存在时才算读取；只提交结果的工具不检查
    world = make_world()
    data = world.layout.data_dir / "issues" / "0018" / "handoff.json"
    world.runner.runs.append(FakeRun(claude_lines(VALID, tools=(
        ("Read", {"file_path": str(data)}), ("Grep", {"pattern": "it.key"}), ("StructuredOutput", {"path": ".env"}),
        ("Read", {"file_path": ".env"})))))
    result = call(world.params(read_paths=(data.parent,)), world.context)
    assert result.status is CallStatus.OK
    (world.workdir / ".env").write_text("TOKEN=x\n", encoding="utf-8")
    world.runner.runs.append(FakeRun(claude_lines(VALID, tools=(("Read", {"file_path": ".env"}),))))
    result = call(world.params(), world.context)
    assert result.violations == ["hidden_read：Read 访问了凭据文件(匹配 .env) .env"]


def test_writes_outside_the_worktree_to_protected_paths_are_a_boundary_violation(make_world):
    def write(*paths: Path) -> Callable[[Command], None]:
        def act(command: Command) -> None:
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("changed\n", encoding="utf-8")
        return act

    world = make_world()
    tool = world.context.tool
    (tool.root / "src" / "tightrein").mkdir(parents=True)
    (tool.root / "src" / "tightrein" / "a.py").write_text("x = 1\n", encoding="utf-8")
    world.runner.runs.append(FakeRun(codex_lines(), action=write(
        world.layout.settings, tool.root / "src" / "tightrein" / "a.py",
        tool.root / "src" / ".venv" / "lib" / "b.py", tool.root / "src" / "tightrein" / "__pycache__" / "a.pyc",
        world.workdir / "src" / "c.py")))
    result = call(world.codex(), world.context)
    assert (result.status, result.attempts) == (CallStatus.BOUNDARY, 1)
    assert result.violations == [f"outside：agent 不可写的 {tool.root / 'src' / 'tightrein' / 'a.py'} 被修改",
                                 f"outside：agent 不可写的 {world.layout.settings} 被修改"]
    # 没有改动的可写调用照常成功(调用前的快照已含上一次的改动)
    world.runner.runs.append(FakeRun(codex_lines()))
    assert call(world.codex(), world.context).status is CallStatus.OK


def test_the_finalize_call_collects_the_structured_result(make_world):
    finalize = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "",
                           "session_id": SESSION, "structured_output": VALID,
                           "usage": {"input_tokens": 50, "output_tokens": 5}, "total_cost_usd": 0.01})
    world = make_world(FakeRun(claude_lines(None)), FakeRun([finalize]))
    result = call(world.params(finalize=True), world.context)
    assert (result.status, result.output, result.attempts) == (CallStatus.OK, VALID, 2)
    first, second = world.runner.commands
    assert "--json-schema" not in first.argv  # 先不带 schema 把事做完
    assert second.argv[1:6] == ("-p", "--resume", SESSION, "--output-format", "json")
    assert "--json-schema" in second.argv and second.stdin.startswith("会话已结束")


def test_an_invalid_finalize_result_is_schema_invalid(make_world):
    bad = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "",
                      "session_id": SESSION, "structured_output": INVALID})
    world = make_world(FakeRun(claude_lines(None)), FakeRun([bad]), FakeRun([bad]))
    result = call(world.params(finalize=True), world.context)
    assert (result.status, result.attempts) == (CallStatus.SCHEMA_INVALID, 3)
    assert world.runner.commands[2].stdin.startswith("上一次的输出不符合")
    assert "--output-format" in world.runner.commands[2].argv


def test_replay_runs_without_processes(make_world, tmp_path):
    world = make_world()
    params = world.params()
    recordings = tmp_path / "recordings"
    (recordings / "triage").mkdir(parents=True)
    (recordings / "triage" / "stdout.jsonl").write_text("\n".join(claude_lines(VALID)), encoding="utf-8")
    (recordings / "index.json").write_text(json.dumps({"recordings": [index_entry(params, 1, "triage", "claude")]}),
                                           encoding="utf-8")
    world.context.replay = ReplayAdapter(recordings, world.context.adapters, world.runner)
    result = call(params, world.context)
    assert (result.status, result.output, result.tokens.output) == (CallStatus.OK, VALID, 100)
    assert world.runner.commands == []
    assert world.context.budget.used("P-0042") == 1100
    assert json.loads(world.file("started", "json").read_text(encoding="utf-8"))["status"] == "ok"
    missing = call(world.params(subject="P-0043"), world.context)
    assert missing.status is CallStatus.FAILED and "replay-missing" in missing.error


def test_programming_errors_raise(make_world):
    world = make_world()
    with pytest.raises(CallConfigError, match="没有工具"):
        call(world.params(model=replace(world.settings.model("opus"), tool="gemini")), world.context)
    with pytest.raises(CallConfigError, match="回放"):
        call(world.params(model=replace(world.settings.model("opus"), tool="replay")), world.context)
    with pytest.raises(CallConfigError, match="联网"):
        call(world.codex(network=True), world.context)


def test_raw_output_is_redacted_and_long_values_truncated(make_world):
    world = make_world()
    world.redactor.register("Pa55-w0rd!")
    long_line = json.dumps({"type": "user", "message": {"content": "x" * 20_000 + "Pa55-w0rd!"}})
    world.runner.runs.append(FakeRun(["plain text Pa55-w0rd!", long_line], exit_code=1, stderr="boom"))
    result = call(world.params(fallback=None), world.context)
    text = result.raw_path.read_text(encoding="utf-8")
    assert "Pa55-w0rd!" not in text and "plain text" in text  # 不认识的行原样保留(脱敏后)
    content = json.loads(text.splitlines()[-1])["message"]["content"]
    assert len(content) == 16384 and content.endswith(TRUNCATED)


# 结构化结果的提取


@pytest.mark.parametrize("text, expected", [
    ('{"verdict": "confirmed"}', {"verdict": "confirmed"}),
    ('  [1, 2]  ', [1, 2]),
    (f'结论如下：\n{FENCE}json\n{{"a": 1}}\n{FENCE}\n再给一个：\n{FENCE}json\n{{"a": 2}}\n{FENCE}\n', {"a": 2}),
    ('我认为 {"verdict": "refuted", "note": "括号 } 在字符串里"} 就是结论', {"verdict": "refuted", "note": "括号 } 在字符串里"}),
    ('先 {"a": 1} 后 {"b": {"c": 2}} 完', {"b": {"c": 2}}),
    (f'{FENCE}json\n{{坏的}}\n{FENCE}\n但是 {{"ok": true}}', {"ok": True}),
    ('转义 {"q": "a \\" b"}', {"q": 'a " b'}),
])
def test_extract_json(text, expected):
    assert extract_json(text) == expected


@pytest.mark.parametrize("text", [None, "", "没有 JSON", "{没闭合", f"{FENCE}json\n{{坏}}\n{FENCE}"])
def test_no_json(text):
    assert extract_json(text) is None


def test_structured_output_is_preferred_and_must_be_an_object():
    output, errors = checked_output(Parsed(CallStatus.OK, text='{"y": 2}', structured={"x": 1}), SCHEMA)
    assert output is None and any("sameRootCause" in error for error in errors)
    assert checked_output(Parsed(CallStatus.OK, text="没有结果"), SCHEMA) == (None, ["/：输出中没有可解析的 JSON 对象"])
    assert checked_output(Parsed(CallStatus.OK, text="[1, 2]"), SCHEMA) == (None, ["/：结果须为 JSON 对象"])
    assert checked_output(Parsed(CallStatus.OK, text=json.dumps(VALID)), SCHEMA) == (VALID, [])


def test_the_retry_note_lists_errors_and_the_raw_output():
    note = retry_note('{"verdict": "maybe"}', ["/verdict：'maybe' is not one of [...]"])
    assert note.splitlines()[0].startswith("上一次的输出不符合要求的 schema")
    assert "- /verdict：'maybe' is not one of [...]" in note and note.endswith('{"verdict": "maybe"}')
    assert retry_note(None, ["/：x"]).endswith("(空)")
    assert len(retry_note("y" * 9000, ["/：x"]).splitlines()[-1]) == 8000
    assert raw_output(Parsed(CallStatus.OK, text="", structured={"ok": "是"})) == '{"ok": "是"}'
    assert raw_output(Parsed(CallStatus.OK, text="文本")) == "文本"


def test_truncation_never_exceeds_the_limit():
    assert truncate("x" * 1000, 5) == "xxxxx"
    assert len(truncate("x" * 1000, 100)) == 100
    assert truncate("short", 100) == "short"
