from datetime import datetime, timezone
from pathlib import Path

import pytest
from runner_samples import ENV, fixture, invocation_files, task

from tightrein.domain.enums import Access
from tightrein.runner.adapters.base import RetryContext, SessionRef, to_events
from tightrein.runner.adapters.claude import ClaudeAdapter
from tightrein.runner.result import Usage
from tightrein.runner.task import Limits

SESSION = "0d9f8e7c-6b5a-4c3d-2e1f-0a9b8c7d6e5f"


@pytest.fixture
def adapter(tmp_path):
    return ClaudeAdapter(tmp_path / ".claude", new_id=lambda: SESSION)


def test_read_only_invocation(adapter, tmp_path):
    files = invocation_files(tmp_path / "raw")
    invocation = adapter.build(task(), files, executable="/usr/local/bin/claude", model="claude-opus", env=ENV,
                               retry=None)
    assert invocation.argv == (
        "/usr/local/bin/claude", "-p", "--output-format", "stream-json", "--verbose",
        "--tools", "Read,Grep,Glob,Bash", "--allowedTools", "Read,Grep,Glob,Bash(git log *),Bash(git show *)",
        "--permission-mode", "dontAsk", "--max-turns", "40", "--model", "claude-opus",
        "--json-schema", '{"type": "object"}',
    )
    assert invocation.stdin == files.prompt.read_bytes()
    assert (invocation.cwd, invocation.env) == (Path("/ws/worktrees/readonly"), ENV)
    effort = adapter.build(task(), files, executable="claude", model="claude-opus", env=ENV, retry=None, effort="high")
    assert effort.argv[effort.argv.index("--model"):effort.argv.index("--model") + 4] == (
        "--model", "claude-opus", "--effort", "high")


def test_workspace_write_web_and_read_paths(adapter, tmp_path):
    files = invocation_files(tmp_path / "raw")
    writer = task(access=Access.WORKSPACE_WRITE, allowed_commands=("dotnet build",), web=True, output_schema=None,
                  limits=Limits(max_cost_usd=2.5), read_paths=("/shots/a.png", "/shots/b.png"))
    argv = adapter.build(writer, files, executable="claude", model=None, env=ENV, retry=None).argv
    assert argv[5:] == (
        "--tools", "Read,Grep,Glob,Bash,Edit,Write,WebSearch,WebFetch",
        "--allowedTools", "Read,Grep,Glob,Bash(dotnet build *),WebSearch,WebFetch",
        "--permission-mode", "acceptEdits", "--add-dir", "/shots", "--max-budget-usd", "2.5",
    )


def test_format_retry_resumes_the_session(adapter, tmp_path):
    files = invocation_files(tmp_path / "raw", call=2)
    invocation = adapter.build(task(), files, executable="claude", model=None, env=ENV,
                               retry=RetryContext("按 schema 重新输出", "5f0c"))
    assert invocation.argv[-2:] == ("--resume", "5f0c")
    assert invocation.stdin == "按 schema 重新输出".encode()


def test_parse_a_successful_run(adapter):
    parsed = adapter.parse(fixture("claude", "stream-success.jsonl"), 0)
    assert parsed.session_id == "5f0c2a1e-8d3b-4c6e-9a41-7b2d9e0f1c3a"
    assert (parsed.structured, parsed.final_text) == ({"verdict": "confirmed"}, '{"verdict": "confirmed"}')
    assert parsed.usage == Usage(4300, 100, 2800, 0.0421, False)
    assert (parsed.turns, parsed.ended_by, parsed.error_message) == (2, "completed", None)


def test_parse_native_limits_and_missing_results(adapter, tmp_path):
    parsed = adapter.parse(fixture("claude", "stream-max-turns.jsonl"), 1)
    assert (parsed.ended_by, parsed.turns) == ("turn-limit", 40)
    empty = tmp_path / "stdout.jsonl"
    empty.write_text('{"type":"system","subtype":"init","session_id":"s"}\n', encoding="utf-8")
    missing = adapter.parse(empty, 1)
    assert (missing.ended_by, missing.session_id, missing.error_message) == ("error", "s", "输出中没有 result 事件")


def test_events_follow_the_unified_mapping(adapter):
    drafts = list(to_events(adapter, fixture("claude", "stream-success.jsonl")))
    assert [(draft.type, draft.actor) for draft in drafts] == [
        ("session-start", "system"), ("message", "assistant"), ("tool-call", "assistant"), ("tool-result", "user"),
        ("error", "system"), ("message", "system"), ("message", "assistant"), ("usage", "system"),
        ("result", "assistant"),
    ]
    assert drafts[0].session_id == "5f0c2a1e-8d3b-4c6e-9a41-7b2d9e0f1c3a"
    assert (drafts[2].tool_name, drafts[2].tool_input, drafts[2].tool_call_id) == (
        "Read", {"file_path": "src/Services/OrderService.cs"}, "toolu_01",
    )
    assert drafts[3].tool_output.startswith("public IList<Order>")
    assert drafts[4].text == "API 重试 第 1 次：overloaded_error(状态 529)"
    assert '"thinking"' in drafts[5].text
    assert adapter.convert("不是 JSON")[0].text == "不是 JSON"
    assert adapter.closing_events(adapter.parse(fixture("claude", "stream-success.jsonl"), 0)) == []


def test_error_results_add_an_error_event(adapter):
    drafts = list(to_events(adapter, fixture("claude", "stream-max-turns.jsonl")))
    assert [draft.type for draft in drafts] == ["session-start", "usage", "result", "error"]


def test_interactive_start_and_resume(adapter, tmp_path):
    files = invocation_files(tmp_path / "raw")
    session = SessionRef(adapter.new_session_id())
    session_task = task(interactive=True, output_schema=None, role="fix-session")
    start = adapter.build_interactive(session_task, files, "Issue 文件：issues/0007.md", executable="claude",
                                      model=None, env=ENV, session=session, resume=False)
    assert start.argv[:5] == ("claude", "--session-id", SESSION, "--append-system-prompt-file", str(files.prompt))
    assert start.argv[-2:] == ("--", "Issue 文件：issues/0007.md")
    assert start.stdin is None
    resume = adapter.build_interactive(session_task, files, "继续", executable="claude", model="claude-opus",
                                       env=ENV, session=session, resume=True)
    assert resume.argv[:3] == ("claude", "--resume", SESSION)
    assert "--append-system-prompt-file" not in resume.argv
    with pytest.raises(ValueError):
        adapter.build_interactive(session_task, files, "x", executable="claude", model=None, env=ENV, session=None,
                                  resume=False)


def test_session_file_is_read_after_the_session(adapter, tmp_path):
    workdir = Path("/ws/worktrees/fix-0007")
    path = adapter.session_file(workdir, SESSION)
    assert path == tmp_path / ".claude" / "projects" / "-ws-worktrees-fix-0007" / f"{SESSION}.jsonl"
    started = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
    assert adapter.locate_session(workdir, started, SESSION) == SessionRef(SESSION, None)
    path.parent.mkdir(parents=True)
    path.write_text(fixture("claude", "session.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
    session = adapter.locate_session(workdir, started, SESSION)
    drafts = adapter.session_events(session)
    assert [draft.type for draft in drafts] == [
        "message", "message", "tool-call", "usage", "tool-result", "message", "usage", "message",
    ]
    assert drafts[1].timestamp == datetime(2026, 10, 5, 3, 0, 5, 120000, tzinfo=timezone.utc)
    assert drafts[4].tool_output == "修复计划已写入 data/fixes/0007/plan.md"
    assert Usage.total(draft.usage for draft in drafts if draft.usage) == Usage(12200, 100, 6000, None, False)
    assert adapter.locate_session(workdir, started, None) is None
    assert adapter.session_events(SessionRef(SESSION)) == []


def test_finalize_call_resumes_with_a_schema(adapter, tmp_path):
    files = invocation_files(tmp_path / "raw")
    invocation = adapter.build_finalize(task(), files, SessionRef(SESSION), executable="claude", model=None, env=ENV)
    assert invocation.argv == ("claude", "-p", "--resume", SESSION, "--output-format", "json", "--json-schema",
                               '{"type": "object"}')
    parsed = adapter.parse(fixture("claude", "finalize.json"), 0)
    assert (parsed.structured, parsed.ended_by) == ({"summary": "会话中生成了修复计划"}, "completed")


def test_schema_dialect_is_dropped_for_the_cli(adapter, tmp_path):
    files = invocation_files(tmp_path / "raw", schema='{"$schema": "https://json-schema.org/draft/2020-12/schema", '
                                                      '"type": "object", "title": "候选主张"}')
    invocation = adapter.build(task(), files, executable="claude", model=None, env=ENV, retry=None)
    assert invocation.argv[invocation.argv.index("--json-schema") + 1] == '{"type": "object", "title": "候选主张"}'
    finalize = adapter.build_finalize(task(), files, SessionRef(SESSION), executable="claude", model=None, env=ENV)
    assert finalize.argv[-1] == '{"type": "object", "title": "候选主张"}'
