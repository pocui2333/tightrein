import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tightrein.agents.params import Access, CallParams, Limits, Model
from tightrein.agents.result import CallStatus, RateLimit
from tightrein.agents.tools import CallConfigError, Resume
from tightrein.agents.tools.codex import CodexAdapter, unwrap
from tightrein.protocol.handoff import Tokens

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
NOW = datetime(2026, 10, 7, 5, 0, tzinfo=UTC)
ENV = {"PATH": "/usr/bin:/bin"}
GPT = Model("gpt", "codex", "gpt-5")
PROMPT = "# 任务\n判断以下主张是否成立\n"
SCHEMA = {"type": "object"}


def params(**changes) -> CallParams:
    base = CallParams(
        point="implement.code", run="R-20261007T050000Z-implement", subject="0018", model=GPT, fallback=None,
        prompt=PROMPT, schema=SCHEMA, workdir=Path("/ws/worktrees/readonly"),
        limits=Limits(600, 40, 16_000, 50_000, 300),
    )
    return replace(base, **changes)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def build(adapter, scratch, call_params=None, *, model=GPT, schema=SCHEMA, resume=None):
    return adapter.build(call_params or params(), model, executable="codex", env=ENV, schema=schema, scratch=scratch,
                         resume=resume)


@pytest.fixture
def adapter():
    return CodexAdapter()


def test_unattended_invocation(adapter, tmp_path):
    command = build(adapter, tmp_path)
    assert command.argv == (
        "codex", "exec", "--json", "-C", "/ws/worktrees/readonly", "--sandbox", "read-only",
        "-c", "approval_policy=never", "-m", "gpt-5", "--output-schema", str(tmp_path / "schema.json"),
        "-o", str(tmp_path / "last-message.txt"), PROMPT,
    )
    assert command.stdin is None
    effort = build(adapter, tmp_path, model=Model("gpt-high", "codex", "gpt-5", "high"))
    assert effort.argv[9:13] == ("-m", "gpt-5", "-c", "model_reasoning_effort=high")
    writer = build(adapter, tmp_path, params(access=Access.WRITE, read_paths=(Path("/docs"),)), schema=None)
    assert writer.argv[5:7] == ("--sandbox", "workspace-write")
    assert writer.argv[11:13] == ("--add-dir", "/docs")
    assert "--output-schema" not in writer.argv


def test_retry_resumes_and_web_tasks_are_refused(adapter, tmp_path):
    retry = build(adapter, tmp_path, resume=Resume("t1", "重试说明"))
    assert retry.argv[-3:] == ("resume", "t1", "重试说明")
    with pytest.raises(CallConfigError, match="联网"):
        build(adapter, tmp_path, params(network=True))


def test_finalize_resumes_read_only_with_the_schema(adapter, tmp_path):
    command = build(adapter, tmp_path, params(access=Access.WRITE), resume=Resume("t1", "只要 JSON", finalize=True))
    assert command.argv[5:7] == ("--sandbox", "read-only")
    assert "--output-schema" in command.argv and command.argv[-3:] == ("resume", "t1", "只要 JSON")


def test_parse_a_successful_run(adapter):
    parsed = adapter.parse(fixture("exec-success.jsonl"), "", 0, NOW)
    assert (parsed.status, parsed.session_id) == (CallStatus.OK, "0199a213-81c0-7800-8aa1-bbab2a035a53")
    assert (parsed.text, parsed.structured) == ('{"verdict":"confirmed"}', None)
    # codex 的 input_tokens 已含缓存读取，不再相加
    assert parsed.tokens == Tokens(input=24763, output=122, cache_read=24448)
    assert parsed.turns == 2


def test_parse_failures(adapter):
    parsed = adapter.parse(fixture("exec-failed.jsonl"), "", 1, NOW)
    assert (parsed.status, parsed.error) == (
        CallStatus.FAILED, "stream disconnected before completion；stream disconnected before completion")


def test_a_failed_command_inside_the_run_is_not_a_tool_failure(adapter):
    lines = fixture("exec-failed.jsonl").splitlines()[:4] + [
        json.dumps({"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "{}"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 1}}),
    ]
    assert adapter.parse("\n".join(lines), "", 0, NOW).status is CallStatus.OK


def test_lines_give_tool_calls_commands_and_usage(adapter):
    lines = fixture("exec-success.jsonl").splitlines()
    command = adapter.parse_line(lines[3])
    assert (command.tool_calls, command.commands) == (1, ("git log -3 --oneline -- src/Services/OrderService.cs",))
    assert command.tool_inputs[0][0] == "command_execution"
    assert command.tool_inputs[0][1]["command"] == "bash -lc 'git log -3 --oneline -- src/Services/OrderService.cs'"
    mcp = adapter.parse_line(lines[5])
    assert (mcp.tool_calls, mcp.commands) == (1, ())
    assert mcp.tool_inputs[0][1]["arguments"] == {"query": "分页参数"} and "id" not in mcp.tool_inputs[0][1]
    assert adapter.parse_line(lines[-1]).tokens == Tokens(input=24763, output=122, cache_read=24448)
    assert adapter.parse_line(lines[4]).tool_calls == 0  # item.completed 不重复计数


def test_commands_are_unwrapped_from_the_shell():
    assert unwrap("bash -lc 'git grep -n Foo'") == "git grep -n Foo"
    assert unwrap("/bin/zsh -c ls") == "ls"
    assert unwrap("git status") == "git status"
    assert unwrap("bash -lc 'unbalanced") == "bash -lc 'unbalanced"


def test_usage_limit_errors_mean_the_quota_is_used_up(adapter):
    message = ("You've hit your usage limit. Upgrade to Pro (https://openai.com/chatgpt/pricing) "
               "or try again in 2 days 3 hours.")
    stdout = json.dumps({"type": "error", "message": message})
    parsed = adapter.parse(stdout, "", 1, NOW)
    assert parsed.status is CallStatus.QUOTA_EXHAUSTED
    resets = (NOW + timedelta(days=2, hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert parsed.rate_limits == [RateLimit("codex", "weekly", "rejected", 1.0, resets)]
    unknown = adapter.parse("", "You've hit your usage limit.", 1, NOW)
    assert unknown.rate_limits == [RateLimit("codex", "five_hour", "rejected", 1.0, None)]


def test_authentication_failures_and_refusals(adapter):
    assert adapter.parse("", "Error: 401 Unauthorized", 1, NOW).status is CallStatus.AUTH_FAILED
    flagged = json.dumps({"type": "turn.failed", "error": {"message": "content_filter: response was flagged"}})
    assert adapter.parse(flagged, "", 1, NOW).status is CallStatus.REFUSED
