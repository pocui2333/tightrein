import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tightrein.agents.params import Access, CallParams, Limits, Model
from tightrein.agents.result import CallStatus, RateLimit
from tightrein.agents.tools import CallConfigError, Resume
from tightrein.agents.tools.claude import ClaudeAdapter, cli_schema, inline
from tightrein.protocol.handoff import Tokens

FIXTURES = Path(__file__).parent / "fixtures" / "claude"
NOW = datetime(2026, 10, 7, 5, 0, tzinfo=UTC)  # 东京 14:00
ENV = {"PATH": "/usr/bin:/bin"}
SESSION = "5f0c2a1e-8d3b-4c6e-9a41-7b2d9e0f1c3a"
OPUS = Model("opus", "claude", "opus", "high")
SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "title": "候选主张"}


def params(**changes) -> CallParams:
    base = CallParams(
        point="assess.triage", run="R-20261007T050000Z-assess", subject="P-0042", model=OPUS, fallback=None,
        prompt="# 任务\n判断以下主张是否成立\n", schema=SCHEMA, workdir=Path("/ws/worktrees/readonly"),
        allowed_commands=("git log", "git show"), limits=Limits(600, 40, 16_000, 50_000, 300),
    )
    return replace(base, **changes)


def build(adapter, call_params=None, *, schema=SCHEMA, resume=None, model=OPUS, tmp_path=Path("/tmp")):
    return adapter.build(call_params or params(), model, executable="/usr/local/bin/claude", env=ENV, schema=schema,
                         scratch=tmp_path, resume=resume)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def result_line(text: str, *, subtype: str = "success", is_error: bool = True, **extra) -> str:
    return json.dumps({"type": "result", "subtype": subtype, "is_error": is_error, "result": text,
                       "session_id": SESSION, "usage": {"input_tokens": 10, "output_tokens": 1}, **extra})


@pytest.fixture
def adapter():
    return ClaudeAdapter()


def test_read_only_invocation(adapter):
    command = build(adapter)
    assert command.argv == (
        "/usr/local/bin/claude", "-p", "--output-format", "stream-json", "--verbose",
        "--tools", "Read,Grep,Glob,Bash", "--allowedTools", "Read,Grep,Glob,Bash(git log *),Bash(git show *)",
        "--permission-mode", "dontAsk", "--max-turns", "40", "--model", "opus", "--effort", "high",
        "--json-schema", '{"type": "object", "title": "候选主张"}',
    )
    assert command.stdin == "# 任务\n判断以下主张是否成立\n"  # 提示经标准输入，不受命令行长度限制
    assert (command.cwd, command.env) == (Path("/ws/worktrees/readonly"), ENV)


def test_workspace_write_web_and_read_paths(adapter):
    writer = params(access=Access.WRITE, allowed_commands=("dotnet build",), network=True,
                    read_paths=(Path("/shots"), Path("/docs"), Path("/shots")))
    argv = build(adapter, writer, schema=None, model=Model("sonnet", "claude", "sonnet")).argv
    assert argv[5:] == (
        "--tools", "Read,Grep,Glob,Bash,Edit,Write,WebSearch,WebFetch",
        "--allowedTools", "Read,Grep,Glob,Bash(dotnet build *),WebSearch,WebFetch",
        "--permission-mode", "acceptEdits", "--add-dir", "/shots", "--add-dir", "/docs",
        "--max-turns", "40", "--model", "sonnet",
    )


def test_format_retry_resumes_the_session(adapter):
    command = build(adapter, resume=Resume("5f0c", "按 schema 重新输出"))
    assert command.argv[-2:] == ("--resume", "5f0c")
    assert command.stdin == "按 schema 重新输出"


def test_finalize_call_resumes_with_a_schema(adapter):
    command = build(adapter, resume=Resume(SESSION, "只要 JSON", finalize=True), model=Model("o", "claude", "opus"))
    assert command.argv == ("/usr/local/bin/claude", "-p", "--resume", SESSION, "--output-format", "json",
                            "--json-schema", '{"type": "object", "title": "候选主张"}', "--model", "opus")
    assert command.stdin == "只要 JSON"
    parsed = adapter.parse(fixture("finalize.json"), "", 0, NOW)
    assert (parsed.status, parsed.structured) == (CallStatus.OK, {"summary": "会话中生成了修复计划"})


def test_unattended_output_is_capped_through_the_environment(adapter):
    assert adapter.env_values(params()) == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "16000"}
    assert "ANTHROPIC_API_KEY" not in adapter.env_names


def test_schema_dialect_is_dropped_and_references_are_inlined():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/item"}},
                       "note": {"$ref": "#/$defs/code", "maxLength": 3}},
        "$defs": {"item": {"type": "object", "properties": {"code": {"$ref": "#/$defs/code"}}},
                  "code": {"type": "string", "pattern": "^[A-Z]+$", "title": "代码"}},
    }
    text = cli_schema(schema)
    assert "$ref" not in text and "$schema" not in text and "$defs" not in text
    inlined = json.loads(text)
    assert inlined["properties"]["items"]["items"]["properties"]["code"] == {"type": "string", "pattern": "^[A-Z]+$"}
    assert inlined["properties"]["note"] == {"maxLength": 3, "allOf": [{"type": "string", "pattern": "^[A-Z]+$"}]}


def test_recursive_schemas_are_rejected():
    with pytest.raises(CallConfigError, match="循环"):
        inline({"type": "object", "properties": {"child": {"$ref": "#"}}})
    with pytest.raises(CallConfigError):
        inline({"type": "object", "properties": {"a": {"$ref": "other.json#/x"}}})


def test_parse_a_successful_run(adapter):
    parsed = adapter.parse(fixture("stream-success.jsonl"), "", 0, NOW)
    assert (parsed.status, parsed.session_id) == (CallStatus.OK, SESSION)
    assert (parsed.structured, parsed.text) == ({"verdict": "confirmed"}, '{"verdict": "confirmed"}')
    # 输入 = 未缓存 1500 + 写缓存 0 + 读缓存 2800；读缓存另记
    assert parsed.tokens == Tokens(input=4300, output=100, cache_read=2800, cache_write=0)
    assert (parsed.cost_usd, parsed.turns, parsed.error) == (0.0421, 2, None)


def test_parse_native_limits_and_missing_results(adapter):
    parsed = adapter.parse(fixture("stream-max-turns.jsonl"), "", 1, NOW)
    assert (parsed.status, parsed.turns) == (CallStatus.TURN_LIMIT, 40)
    budget = result_line("", subtype="error_max_budget_usd")
    assert adapter.parse(budget, "", 1, NOW).status is CallStatus.BUDGET_LIMIT
    missing = adapter.parse('{"type":"system","subtype":"init","session_id":"s"}\n', "", 1, NOW)
    assert (missing.status, missing.session_id, missing.error) == (CallStatus.FAILED, "s", "输出中没有 result 事件")


def test_lines_count_tool_calls_and_deduplicate_usage(adapter):
    lines = fixture("stream-success.jsonl").splitlines()
    first = adapter.parse_line(lines[1])
    assert (first.tool_calls, first.usage_id) == (1, "msg_01")
    assert first.tool_inputs == (("Read", {"file_path": "src/Services/OrderService.cs"}),)
    assert first.tokens == Tokens(input=2000, output=60, cache_read=800)
    assert adapter.parse_line(lines[3]).tool_calls == 0  # api_retry
    assert adapter.parse_line("不是 JSON").tokens is None


def test_rate_limit_events_are_read(adapter):
    line = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning", "resetsAt": 1791349200, "rateLimitType": "seven_day_opus", "utilization": 0.83}})
    assert adapter.parse_line(line).rate_limit == RateLimit("claude", "opus", "warning", 0.83, "2026-10-07T05:00:00Z")
    percent = json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "utilization": 42}})
    assert adapter.parse_line(percent).rate_limit == RateLimit("claude", "five_hour", "allowed", 0.42, None)
    stdout = "\n".join([line, fixture("stream-success.jsonl")])
    assert adapter.parse(stdout, "", 0, NOW).rate_limits[0].window == "opus"


def test_session_limit_errors_mean_the_quota_is_used_up(adapter):
    parsed = adapter.parse(result_line("You've hit your session limit · resets 3:45pm (Asia/Tokyo)"), "", 1, NOW)
    assert parsed.status is CallStatus.QUOTA_EXHAUSTED
    assert parsed.rate_limits == [RateLimit("claude", "five_hour", "rejected", 1.0, "2026-10-07T06:45:00Z")]
    earlier = adapter.parse(result_line("You've hit your session limit · resets 1pm (Asia/Tokyo)"), "", 1, NOW)
    assert earlier.rate_limits[0].resets_at == "2026-10-08T04:00:00Z"  # 今天 13 点已过，取明天


def test_weekly_and_model_limits(adapter):
    weekly = adapter.parse(result_line("You've hit your weekly limit · resets Oct 9, 3pm (Asia/Tokyo)"), "", 1, NOW)
    assert weekly.rate_limits == [RateLimit("claude", "weekly", "rejected", 1.0, "2026-10-09T06:00:00Z")]
    opus = adapter.parse("", "You've hit your Opus limit · resets Oct 12 at 9am (UTC)", 1, NOW)
    assert (opus.status, opus.rate_limits[0].window, opus.rate_limits[0].resets_at) == (
        CallStatus.QUOTA_EXHAUSTED, "opus", "2026-10-12T09:00:00Z")
    legacy = adapter.parse(result_line("Claude AI usage limit reached|1791349200"), "", 1, NOW)
    assert (legacy.status, legacy.rate_limits[0].resets_at) == (CallStatus.QUOTA_EXHAUSTED, "2026-10-07T05:00:00Z")


def test_a_rejected_rate_limit_event_marks_the_failure_as_quota(adapter):
    rejected = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": 1791349200, "rateLimitType": "five_hour"}})
    parsed = adapter.parse("\n".join([rejected, result_line("API Error: 429")]), "", 1, NOW)
    assert parsed.status is CallStatus.QUOTA_EXHAUSTED


def test_authentication_failures_are_recognised(adapter):
    assert adapter.parse("not json", "Invalid API key · Please run /login", 1, NOW).status is CallStatus.AUTH_FAILED
    expired = result_line('API Error: 401 {"type":"error","error":{"type":"authentication_error"}}')
    assert adapter.parse(expired, "", 1, NOW).status is CallStatus.AUTH_FAILED


def test_safety_refusals_are_recognised(adapter):
    text = "API Error: Claude Code is unable to respond to this request, which appears to violate our Usage Policy"
    assert adapter.parse(result_line(text), "", 1, NOW).status is CallStatus.REFUSED
    refusal = result_line("", is_error=False, stop_reason="refusal")
    assert adapter.parse(refusal, "", 0, NOW).status is CallStatus.REFUSED


def test_other_errors_stay_failures(adapter):
    parsed = adapter.parse(result_line("something broke"), "boom", 1, NOW)
    assert (parsed.status, parsed.error) == (CallStatus.FAILED, "success：something broke")
