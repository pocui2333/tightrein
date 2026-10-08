import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tightrein.agents.params import Access, CallParams, Limits, Model
from tightrein.agents.result import CallStatus, RateLimit
from tightrein.agents.tools import Resume
from tightrein.agents.tools.agy import NO_BROWSER_DIR, SHELL_NOTE, TURNS_NOTE, WEB_NOTE, AgyAdapter, allowed
from tightrein.protocol.handoff import Tokens

FIXTURES = Path(__file__).parent / "fixtures" / "agy"
NOW = datetime(2026, 10, 7, 5, 0, tzinfo=UTC)
ENV = {"PATH": "/usr/bin:/bin"}
SUCCESS_ID = "22b76f42-e99d-4724-b793-90aeea6139f6"
TOOLS_ID = "29ff8053-3b71-4a70-a242-6773efc6fe25"
FLASH = Model("flash-high", "agy", "gemini-3.8-flash-high", "high")
PROMPT = "# 任务\n判断以下主张是否成立\n"
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


def params(**changes) -> CallParams:
    base = CallParams(
        point="assess.refute", run="R-20261007T050000Z-assess", subject="P-0042", model=FLASH, fallback=None,
        prompt=PROMPT, schema=SCHEMA, workdir=Path("/ws/worktrees/readonly"),
        allowed_commands=("git grep", "cat", "git push"), limits=Limits(600, 40, 16_000, 50_000, 300),
    )
    return replace(base, **changes)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def adapter(tmp_path):
    return AgyAdapter(tmp_path / "no-settings.json")  # 没有白名单：说明 shell 不可用


@pytest.fixture
def allow_list(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"permissions": {"allow": [
        "command(git grep)", "command(cat)", "command(rm)", "read(/tmp)"]}}), encoding="utf-8")
    return path


def test_readonly_runs_in_the_sandbox_with_the_schema_file(adapter, tmp_path):
    command = adapter.build(params(read_paths=(Path("/shots"),)), FLASH, executable="agy", env=ENV, schema=SCHEMA,
                            scratch=tmp_path, resume=None)
    assert command.argv == (
        "agy", "--sandbox", "--model", "gemini-3.8-flash-high", "--effort", "high", "--add-dir", "/shots",
        "--output-format", "stream-json", "--json-schema", str(tmp_path / "schema.json"),
        "-p", PROMPT + SHELL_NOTE + TURNS_NOTE.format(turns=40),
    )
    assert json.loads((tmp_path / "schema.json").read_text(encoding="utf-8")) == SCHEMA
    assert "--dangerously-skip-permissions" not in command.argv
    assert (command.cwd, command.stdin) == (Path("/ws/worktrees/readonly"), None)  # 提示只能作为 -p 的参数


def test_allow_listed_read_commands_are_offered_with_the_tool_call_limit(allow_list, tmp_path):
    assert allowed(allow_list) == ("git grep", "cat", "rm")
    command = AgyAdapter(allow_list).build(params(limits=Limits(600, 60, 16_000, 50_000, 300)), FLASH,
                                           executable="agy", env=ENV, schema=None, scratch=tmp_path, resume=None)
    prompt = command.argv[-1]
    # 只列白名单与本次放行命令的交集：rm 没有放行，git push 不在 agy 白名单
    assert "可以执行这些只读命令：`git grep`、`cat`。" in prompt
    assert "git push" not in prompt and "`rm`" not in prompt
    assert "先用 `git grep -n" in prompt and "工具调用最多 60 次" in prompt and "shell 命令会被自动拒绝" not in prompt


def test_each_command_must_run_on_its_own(allow_list, tmp_path):
    """agy 按整条命令匹配白名单：用 && 等连起来的命令只要有一条不在白名单，整条被拒、整轮作废。"""
    prompt = AgyAdapter(allow_list).build(params(), FLASH, executable="agy", env=ENV, schema=None, scratch=tmp_path,
                                          resume=None).argv[-1]
    assert "每次只执行一条命令，不要用 `&&`、`||`、`;`、`|` 把多条命令连在一起" in prompt
    assert "读过就不要重复打开" in prompt


def test_web_tasks_are_told_to_answer_from_search_results_only(adapter, tmp_path):
    command = adapter.build(params(network=True), FLASH, executable="agy", env=ENV, schema=None, scratch=tmp_path,
                            resume=None)
    assert command.argv[-1] == PROMPT + SHELL_NOTE + TURNS_NOTE.format(turns=40) + WEB_NOTE


def test_writable_tasks_accept_edits_and_retries_continue_the_conversation(adapter, tmp_path):
    command = adapter.build(params(access=Access.WRITE), Model("f", "agy", ""), executable="agy", env=ENV,
                            schema=None, scratch=tmp_path, resume=Resume(SUCCESS_ID, "按 schema 重新输出"))
    assert command.argv == ("agy", "--mode", "accept-edits", "--output-format", "stream-json",
                            "--conversation", SUCCESS_ID, "-p", "按 schema 重新输出")
    assert "--sandbox" not in command.argv and "--dangerously-skip-permissions" not in command.argv


def test_finalize_resumes_with_the_schema(adapter, tmp_path):
    command = adapter.build(params(), FLASH, executable="agy", env=ENV, schema=SCHEMA, scratch=tmp_path,
                            resume=Resume(SUCCESS_ID, "只要 JSON", finalize=True))
    assert command.argv[-6:] == ("--json-schema", str(tmp_path / "schema.json"), "--conversation", SUCCESS_ID,
                                 "-p", "只要 JSON")


def test_unattended_calls_cannot_open_a_browser(adapter, tmp_path):
    """agy 读取登录凭据失败时会调用 open 弹出浏览器登录页；PATH 最前面的 open 什么都不做。"""
    command = adapter.build(params(), FLASH, executable="agy", env={"PATH": "/usr/bin", "HOME": "/h"}, schema=None,
                            scratch=tmp_path, resume=None)
    assert command.env["PATH"] == f"{NO_BROWSER_DIR}:/usr/bin"
    assert command.env["HOME"] == "/h"
    for name in ("open", "xdg-open"):
        assert (NO_BROWSER_DIR / name).read_text(encoding="utf-8").rstrip().endswith("exit 1")


def test_parse_takes_the_structured_output_and_sums_step_usage(adapter):
    parsed = adapter.parse(fixture("stream-success.jsonl"), "", 0, NOW)
    assert (parsed.status, parsed.session_id, parsed.structured, parsed.turns) == (
        CallStatus.OK, SUCCESS_ID, {"ok": True}, 0)
    assert parsed.tokens == Tokens(input=26258, output=93)
    assert parsed.text.startswith("ok true\n")
    assert parsed.cost_usd is None  # agy 不报费用，由 agents/call.py 按价格估算


def test_a_resumed_conversation_counts_only_this_call(adapter):
    stdout = fixture("stream-resumed.jsonl")
    parsed = adapter.parse(stdout, "", 0, NOW)
    assert (parsed.session_id, parsed.structured) == (SUCCESS_ID, {"ok": True, "word": "again"})
    assert json.loads(stdout.splitlines()[-1])["result"]["usage"]["input_tokens"] == 40591  # 整个会话的累计
    assert parsed.tokens.input == 14333


def test_json_output_is_one_object(adapter):
    parsed = adapter.parse(fixture("output.json"), "", 0, NOW)
    assert (parsed.session_id, parsed.structured, parsed.status) == (
        "8702d5e2-ffb7-4134-916d-b034ede1553c", {"ok": True}, CallStatus.OK)
    assert parsed.tokens == Tokens(input=13051, output=50)


def test_errors_and_missing_results(adapter):
    failed = adapter.parse(fixture("stream-error.jsonl"), "", 1, NOW)
    assert (failed.session_id, failed.status) == (None, CallStatus.FAILED)
    assert failed.error.startswith('invalid model selection (--model "no-such-model"')
    cut = "\n".join(fixture("stream-success.jsonl").splitlines()[:3])
    missing = adapter.parse(cut, "", 0, NOW)
    assert (missing.session_id, missing.status, missing.error) == (SUCCESS_ID, CallStatus.FAILED,
                                                                   "输出中没有 result 事件")


def test_denied_actions_are_reported(adapter):
    parsed = adapter.parse(fixture("stream-tools.jsonl"), "", 0, NOW)
    assert (parsed.status, parsed.turns, parsed.structured, parsed.text) == (CallStatus.OK, 2, None, "")
    assert parsed.error == "无人值守模式自动拒绝了需要确认的动作：write_file"


def test_cache_reads_are_part_of_the_input(adapter):
    line = {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "agent_response",
                                                    "usage": {"input_tokens": 45265, "output_tokens": 3080,
                                                              "cache_read_tokens": 61026}}}
    assert adapter.parse_line(json.dumps(line)).tokens == Tokens(input=106291, output=3080, cache_read=61026)


def test_finished_tool_steps_count_as_tool_calls(adapter):
    events = [adapter.parse_line(line) for line in fixture("stream-tools.jsonl").splitlines()]
    assert sum(event.tool_calls for event in events) == 2  # ACTIVE 不算，DONE 与 ERROR 各算一次
    # 工具参数在 ACTIVE 时就带出，供程序检查读取越界
    assert events[3].tool_inputs == (("run_command", {"CommandLine": "ls"}),)
    assert events[6].tool_inputs == (("write_to_file", {"TargetFile": "/ws/worktrees/fix-0007/c.txt"}),)
    assert adapter.parse_line("不是 JSON").tool_calls == 0


def test_individual_quota_errors_mean_the_quota_is_used_up(adapter):
    stderr = "Error: Individual quota reached. Resets in 143h57m55s."
    parsed = adapter.parse("", stderr, 1, NOW)
    assert parsed.status is CallStatus.QUOTA_EXHAUSTED
    resets = NOW + timedelta(hours=143, minutes=57, seconds=55)
    assert parsed.rate_limits == [RateLimit("agy", "weekly", "rejected", 1.0, resets.strftime("%Y-%m-%dT%H:%M:%SZ"))]
    soon = adapter.parse("", "Individual quota reached. Resets in 2h10m", 1, NOW)
    assert soon.rate_limits[0].window == "five_hour"


def test_authentication_failures_and_refusals(adapter):
    assert adapter.parse("", "Error: not logged in. Run agy to sign in.", 1, NOW).status is CallStatus.AUTH_FAILED
    blocked = json.dumps({"event": "result", "result": {"status": "ERROR", "error": "response blocked due to safety"}})
    assert adapter.parse(blocked, "", 1, NOW).status is CallStatus.REFUSED
