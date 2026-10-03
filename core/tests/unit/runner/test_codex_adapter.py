from datetime import datetime, timezone
from pathlib import Path

import pytest
from runner_samples import ENV, fixture, invocation_files, task

from tightrein.domain.enums import Access
from tightrein.runner.adapters.base import RetryContext, SessionRef, to_events
from tightrein.runner.adapters.codex import CodexAdapter
from tightrein.runner.result import RunnerConfigError, Usage

SESSION = "0199b000-1111-7222-8333-444455556666"
STARTED = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def adapter(tmp_path):
    return CodexAdapter(tmp_path / ".codex")


def test_unattended_invocation(adapter, tmp_path):
    files = invocation_files(tmp_path / "raw")
    invocation = adapter.build(task(), files, executable="codex", model="gpt-5", env=ENV, retry=None)
    assert invocation.argv == (
        "codex", "exec", "--json", "-C", "/ws/worktrees/readonly", "--sandbox", "read-only", "--ask-for-approval",
        "never", "-m", "gpt-5", "--output-schema", str(files.schema), "-o", str(files.last_message),
        "# 任务\n判断以下主张是否成立\n",
    )
    assert invocation.stdin is None
    effort = adapter.build(task(), files, executable="codex", model="gpt-5", env=ENV, retry=None, effort="high")
    assert effort.argv[9:13] == ("-m", "gpt-5", "-c", "model_reasoning_effort=high")
    writer = adapter.build(task(access=Access.WORKSPACE_WRITE, output_schema=None), files, executable="codex",
                           model=None, env=ENV, retry=None)
    assert writer.argv[5:9] == ("--sandbox", "workspace-write", "--ask-for-approval", "never")
    assert "--output-schema" not in writer.argv


def test_retry_resumes_and_web_tasks_are_refused(adapter, tmp_path):
    files = invocation_files(tmp_path / "raw", call=2)
    retry = adapter.build(task(), files, executable="codex", model=None, env=ENV, retry=RetryContext("重试说明", "t1"))
    assert retry.argv[-3:] == ("resume", "t1", "重试说明")
    with pytest.raises(RunnerConfigError):
        adapter.build(task(web=True), files, executable="codex", model=None, env=ENV, retry=None)


def test_parse_a_successful_run(adapter):
    parsed = adapter.parse(fixture("codex", "exec-success.jsonl"), 0)
    assert parsed.session_id == "0199a213-81c0-7800-8aa1-bbab2a035a53"
    assert (parsed.final_text, parsed.structured) == ('{"verdict":"confirmed"}', None)
    assert parsed.usage == Usage(24763, 122, 24448, None, False)
    assert (parsed.turns, parsed.ended_by) == (2, "completed")
    assert [draft.type for draft in adapter.closing_events(parsed)] == ["result"]


def test_parse_failures(adapter):
    parsed = adapter.parse(fixture("codex", "exec-failed.jsonl"), 1)
    assert (parsed.ended_by, parsed.error_message) == (
        "error", "stream disconnected before completion；stream disconnected before completion",
    )
    assert adapter.closing_events(parsed) == []


def test_events_follow_the_unified_mapping(adapter):
    drafts = list(to_events(adapter, fixture("codex", "exec-success.jsonl")))
    assert [(draft.type, draft.actor) for draft in drafts] == [
        ("session-start", "system"), ("message", "assistant"), ("tool-call", "assistant"), ("tool-result", "user"),
        ("tool-call", "assistant"), ("tool-result", "user"), ("message", "assistant"), ("usage", "system"),
    ]
    assert (drafts[2].tool_name, drafts[2].tool_input, drafts[2].tool_call_id) == (
        "shell", {"command": "bash -lc 'git log -3 --oneline -- src/Services/OrderService.cs'"}, "item_1",
    )
    assert (drafts[3].tool_output, drafts[3].is_error) == ("d6f3702 fix: 订单分页\n", False)
    assert (drafts[4].tool_name, drafts[4].tool_input) == ("mcp:tightrein-kb/search", {"query": "分页参数"})
    failed = list(to_events(adapter, fixture("codex", "exec-failed.jsonl")))
    assert [draft.type for draft in failed] == ["session-start", "tool-call", "tool-result", "error", "error"]
    assert failed[2].is_error is True
    assert adapter.convert('{"type":"thread.archived"}')[0].actor == "system"


def test_interactive_start_resume_and_finalize(adapter, tmp_path):
    files = invocation_files(tmp_path / "raw")
    session_task = task(interactive=True, output_schema=None, role="fix-session")
    start = adapter.build_interactive(session_task, files, "Issue 文件：issues/0007.md", executable="codex",
                                      model=None, env=ENV, session=None, resume=False)
    assert start.argv[:7] == ("codex", "-C", "/ws/worktrees/readonly", "--sandbox", "workspace-write",
                              "--ask-for-approval", "on-request")
    assert start.argv[-1].endswith("# 首条输入\nIssue 文件：issues/0007.md")
    resume = adapter.build_interactive(session_task, files, "继续", executable="codex", model=None, env=ENV,
                                       session=SessionRef(SESSION), resume=True)
    assert resume.argv[:3] == ("codex", "resume", SESSION)
    finalize = adapter.build_finalize(task(), files, SessionRef(SESSION), executable="codex", model=None, env=ENV)
    assert finalize.argv[-3:-1] == ("resume", SESSION)
    assert adapter.new_session_id() is None


def write_rollout(adapter, name, cwd, started):
    path = adapter.home / "sessions" / "2026" / "10" / "05" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = fixture("codex", "rollout.jsonl").read_text(encoding="utf-8")
    path.write_text(text.replace("/ws/worktrees/fix-0007", cwd).replace("2026-10-05T03:00:02.000Z", started),
                    encoding="utf-8")
    return path


def test_the_session_is_located_by_workdir_and_start_time(adapter):
    workdir = Path("/ws/worktrees/fix-0007")
    assert adapter.locate_session(workdir, STARTED, None) is None
    write_rollout(adapter, "rollout-a.jsonl", "/ws/worktrees/fix-0008", "2026-10-05T03:00:02.000Z")
    write_rollout(adapter, "rollout-b.jsonl", str(workdir), "2026-10-05T02:00:00.000Z")
    path = write_rollout(adapter, "rollout-c.jsonl", str(workdir), "2026-10-05T03:00:02.000Z")
    assert adapter.locate_session(workdir, STARTED, None) == SessionRef(SESSION, path)


def test_session_events(adapter):
    drafts = adapter.session_events(SessionRef(SESSION, fixture("codex", "rollout.jsonl")))
    assert [(draft.type, draft.actor) for draft in drafts] == [
        ("session-start", "system"), ("message", "user"), ("message", "assistant"), ("tool-call", "assistant"),
        ("tool-result", "user"), ("message", "assistant"), ("usage", "system"), ("message", "system"),
    ]
    assert drafts[3].tool_input == {"command": ["bash", "-lc", "tightrein fix plan 0007"],
                                    "workdir": "/ws/worktrees/fix-0007"}
    assert drafts[4].tool_output == "修复计划已写入 data/fixes/0007/plan.md"
    assert drafts[6].usage == Usage(9000, 300, 4000, None, False)
    assert adapter.session_events(SessionRef(SESSION)) == []
