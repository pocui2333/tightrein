import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from runner_samples import ENV, fixture, invocation_files, task

from tightrein.domain.enums import Access
from tightrein.runner.adapters.agy import SHELL_NOTE, TURNS_NOTE, WEB_NOTE, AgyAdapter, read_commands
from tightrein.runner.task import Limits
from tightrein.runner.adapters.base import RetryContext, SessionRef, to_events
from tightrein.runner.result import RunnerConfigError, Usage

SUCCESS_ID = "22b76f42-e99d-4724-b793-90aeea6139f6"
TOOLS_ID = "29ff8053-3b71-4a70-a242-6773efc6fe25"


@pytest.fixture
def adapter(tmp_path):
    return AgyAdapter(tmp_path / "no-settings.json")  # 没有白名单：说明 shell 不可用


def test_readonly_runs_in_the_sandbox_with_the_schema_file(adapter, tmp_path):
    files = invocation_files(tmp_path / "raw")
    reader = task(read_paths=("/shots/a.png",))
    invocation = adapter.build(reader, files, executable="agy", model="gemini-3.1-pro-high", env=ENV, retry=None,
                               effort="high")
    assert invocation.argv == (
        "agy", "--sandbox", "--model", "gemini-3.1-pro-high", "--effort", "high", "--add-dir", "/shots",
        "--output-format", "stream-json", "--json-schema", str(files.schema),
        "-p", "# 任务\n判断以下主张是否成立\n" + SHELL_NOTE + TURNS_NOTE.format(turns=40),
    )
    assert "--dangerously-skip-permissions" not in invocation.argv
    assert (invocation.cwd, invocation.stdin) == (Path("/ws/worktrees/readonly"), None)


def test_allow_listed_read_commands_are_offered_with_the_tool_call_limit(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": [
        "command(git grep)", "command(cat)", "command(git push -u origin x)", "command(rm)", "read(/tmp)"]}}),
        encoding="utf-8")
    assert read_commands(settings) == ("git grep", "cat")
    files = invocation_files(tmp_path / "raw")
    reader = task().with_limits(Limits(max_turns=60))
    prompt = AgyAdapter(settings).build(reader, files, executable="agy", model=None, env=ENV, retry=None).argv[-1]
    assert "可以执行这些只读命令：`git grep`、`cat`" in prompt and "git push" not in prompt and "`rm`" not in prompt
    assert "先用 `git grep -n" in prompt and "工具调用最多 60 次" in prompt and "shell 命令会被自动拒绝" not in prompt


def test_web_tasks_are_told_to_answer_from_search_results_only(adapter, tmp_path):
    files = invocation_files(tmp_path / "raw")
    invocation = adapter.build(task(web=True), files, executable="agy", model=None, env=ENV, retry=None)
    assert invocation.argv[-1] == "# 任务\n判断以下主张是否成立\n" + SHELL_NOTE + TURNS_NOTE.format(turns=40) + WEB_NOTE


def test_writable_tasks_accept_edits_and_retries_continue_the_conversation(adapter, tmp_path):
    files = invocation_files(tmp_path / "raw", 2)
    writer = task(access=Access.WORKSPACE_WRITE, output_schema=None)
    invocation = adapter.build(writer, files, executable="agy", model=None, env=ENV,
                               retry=RetryContext("按 schema 重新输出", SUCCESS_ID))
    assert invocation.argv == ("agy", "--mode", "accept-edits", "--output-format", "stream-json",
                               "--conversation", SUCCESS_ID, "-p", "按 schema 重新输出")
    assert "--sandbox" not in invocation.argv and "--dangerously-skip-permissions" not in invocation.argv


def test_parse_takes_the_structured_output_and_sums_step_usage(adapter):
    parsed = adapter.parse(fixture("agy", "stream-success.jsonl"), 0)
    assert (parsed.session_id, parsed.structured, parsed.ended_by, parsed.turns) == (
        SUCCESS_ID, {"ok": True}, "completed", 0)
    assert parsed.usage == Usage(26258, 93, 0, None, False)
    assert parsed.final_text.startswith("ok true\n")


def test_a_resumed_conversation_counts_only_this_call(adapter):
    parsed = adapter.parse(fixture("agy", "stream-resumed.jsonl"), 0)
    assert (parsed.session_id, parsed.structured) == (SUCCESS_ID, {"ok": True, "word": "again"})
    result = json.loads(fixture("agy", "stream-resumed.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert result["result"]["usage"]["input_tokens"] == 40591
    assert parsed.usage.input_tokens == 14333


def test_json_output_is_one_object(adapter):
    parsed = adapter.parse(fixture("agy", "output.json"), 0)
    assert (parsed.session_id, parsed.structured, parsed.ended_by) == (
        "8702d5e2-ffb7-4134-916d-b034ede1553c", {"ok": True}, "completed")
    assert parsed.usage == Usage(13051, 50, 0, None, False)
    drafts = list(to_events(adapter, fixture("agy", "output.json")))
    assert [draft.type for draft in drafts] == ["usage", "result"]


def test_errors_and_missing_results(adapter, tmp_path):
    failed = adapter.parse(fixture("agy", "stream-error.jsonl"), 1)
    assert (failed.session_id, failed.ended_by) == (None, "error")
    assert failed.error_message.startswith('invalid model selection (--model "no-such-model"')
    cut = tmp_path / "cut.jsonl"
    cut.write_text("\n".join(fixture("agy", "stream-success.jsonl").read_text(encoding="utf-8").splitlines()[:3]),
                   encoding="utf-8")
    missing = adapter.parse(cut, 0)
    assert (missing.session_id, missing.ended_by, missing.error_message) == (
        SUCCESS_ID, "error", "输出中没有 result 事件")


def test_cache_reads_are_part_of_the_input():
    line = {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "agent_response",
                                                    "usage": {"input_tokens": 45265, "output_tokens": 3080,
                                                              "cache_read_tokens": 61026}}}
    (draft,) = AgyAdapter().convert(json.dumps(line))
    assert draft.usage == Usage(106291, 3080, 61026, None, False)


def test_events_follow_the_unified_mapping(adapter):
    drafts = list(to_events(adapter, fixture("agy", "stream-tools.jsonl")))
    assert [(draft.type, draft.actor) for draft in drafts] == [
        ("session-start", "system"), ("usage", "system"), ("tool-call", "assistant"), ("tool-result", "user"),
        ("usage", "system"), ("tool-call", "assistant"), ("tool-result", "user"), ("result", "assistant"),
        ("error", "system"),
    ]
    assert drafts[0].session_id == TOOLS_ID
    assert (drafts[2].tool_name, drafts[2].tool_input, drafts[2].tool_call_id) == ("run_command", {"CommandLine": "ls"},
                                                                                  "2")
    assert (drafts[3].tool_output, drafts[3].is_error) == ("a.txt\r\n", False)
    assert drafts[6].is_error and drafts[6].tool_output.startswith("permission check failed for write_file")
    assert drafts[8].text == "无人值守模式自动拒绝了需要确认的动作：write_file"
    parsed = adapter.parse(fixture("agy", "stream-tools.jsonl"), 0)
    assert (parsed.turns, parsed.ended_by, parsed.structured, parsed.final_text) == (2, "completed", None, "")
    assert adapter.closing_events(parsed) == []


def test_text_deltas_become_assistant_messages(adapter):
    drafts = list(to_events(adapter, fixture("agy", "stream-success.jsonl")))
    assert [draft.text for draft in drafts if draft.type == "message"] == [
        "ok true", "\n", '{"ok":true,"toolAction":"Finishing task","toolSummary":"Finish task"}\n']
    assert drafts[-1].type == "result"


def test_interactive_sessions_are_not_supported(adapter, tmp_path):
    files = invocation_files(tmp_path / "raw")
    session_task = task(interactive=True, output_schema=None, role="fix-session")
    with pytest.raises(RunnerConfigError, match="routes.fix.session"):
        adapter.new_session_id()
    with pytest.raises(RunnerConfigError):
        adapter.build_interactive(session_task, files, "继续", executable="agy", model=None, env=ENV,
                                  session=SessionRef("x"), resume=True)
    with pytest.raises(RunnerConfigError):
        adapter.build_finalize(task(), files, SessionRef("x"), executable="agy", model=None, env=ENV)
    started = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
    assert adapter.locate_session(Path("/ws"), started, None) is None
    assert adapter.session_events(SessionRef("x")) == []
