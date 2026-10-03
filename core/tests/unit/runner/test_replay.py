import json
from dataclasses import replace

import pytest
from runner_samples import RUN, task

from tightrein.domain.enums import Access, RunnerStatus
from tightrein.runner.adapters.replay import ReplayAdapter
from tightrein.runner.recording import RecordingError, RecordingSet, index_entry, task_sha256
from tightrein.runner.result import Usage
from tightrein.runner.task import Instructions
from tightrein.vcs.process import VcsProcess

RESULT = {
    "status": "ok", "errorType": None, "output": {"verdict": "confirmed"},
    "usage": {"inputTokens": 1200, "outputTokens": 300, "cachedInputTokens": None, "costUsd": 0.02,
              "costEstimated": False},
    "durationMs": 81000, "attempts": 1, "tool": "claude", "model": "claude-opus", "sessionId": "5f0c",
    "transcriptPath": "transcripts/claim-verifier-P-0042.jsonl", "guardReport": None, "violations": [],
}


def event(seq, attempt, kind, **fields):
    return {"seq": seq, "timestamp": "2026-10-05T03:00:00Z", "runId": RUN, "role": "claim-verifier",
            "subjectId": "P-0042", "attempt": attempt, "tool": "claude", "model": None, "sessionId": "5f0c",
            "type": kind, "actor": "assistant", **fields}


def record(root, recorded_task, result=None, events=(), patch=None, first_call=None):
    directory = root / f"{recorded_task.role}-{recorded_task.subject_id}.{recorded_task.attempt}"
    directory.mkdir(parents=True)
    (directory / "result.json").write_text(json.dumps(result or RESULT), encoding="utf-8")
    (directory / "transcript.jsonl").write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
    if patch is not None:
        (directory / "changes.patch").write_text(patch, encoding="utf-8")
    if first_call is not None:
        (directory / "first-call.json").write_text(json.dumps(first_call), encoding="utf-8")
    index = root / "index.json"
    entries = json.loads(index.read_text(encoding="utf-8"))["recordings"] if index.exists() else []
    entries.append(index_entry(recorded_task, directory.name))
    index.write_text(json.dumps({"recordings": entries}), encoding="utf-8")


def adapter(root):
    return ReplayAdapter(RecordingSet(root), VcsProcess(environ={}))


def test_task_hash_covers_prompt_schema_and_access():
    base = task_sha256(task())
    assert len(base) == 64
    assert task_sha256(task(limits=task().limits, attempt=3)) == base
    assert task_sha256(task(instructions=Instructions("另一个提示"))) != base
    assert task_sha256(task(output_schema="runner/roles/refuter.schema.json")) != base
    assert task_sha256(task(access=Access.WORKSPACE_WRITE)) != base


def test_a_recorded_call_is_replayed(tmp_path):
    events = [event(1, 1, "message", text="第一次"), event(2, 1, "tool-call", toolName="Read", toolCallId="t1",
                                                          toolInput={"file_path": "src/A.cs"}),
              event(3, 2, "message", text="重试")]
    record(tmp_path, task(), events=events)
    first = adapter(tmp_path).call(task(), 1)
    assert (first.tool, first.model, first.forced) == ("claude", "claude-opus", None)
    assert (first.parsed.structured, first.parsed.session_id) == ({"verdict": "confirmed"}, "5f0c")
    assert first.parsed.usage == Usage(1200, 300, None, 0.02, False)
    assert [(draft.type, draft.text) for draft in first.events] == [("message", "第一次"), ("tool-call", None)]
    second = adapter(tmp_path).call(task(), 2)
    assert [draft.text for draft in second.events] == ["重试"]
    assert second.parsed.usage == Usage()
    assert adapter(tmp_path).call(task(), 3).events == []


def test_missing_and_changed_recordings(tmp_path):
    record(tmp_path, task())
    missing = adapter(tmp_path).call(task(attempt=2), 1)
    assert missing.forced == (RunnerStatus.FAILED, "replay-missing")
    changed = adapter(tmp_path).call(task(instructions=Instructions("改过的提示")), 1)
    assert changed.forced == (RunnerStatus.FAILED, "replay-task-changed")


def test_recorded_failures_and_first_calls(tmp_path):
    limited = {**RESULT, "status": "limit-reached", "errorType": "timeout", "output": None}
    record(tmp_path, task(), result=limited)
    assert adapter(tmp_path).call(task(), 1).forced == (RunnerStatus.LIMIT_REACHED, "timeout")
    retry_task = task(subject=replace(task().subject, id="P-0043"))
    record(tmp_path, retry_task, first_call={"verdict": "maybe"})
    replayed = adapter(tmp_path)
    assert replayed.call(retry_task, 1).parsed.structured == {"verdict": "maybe"}
    assert replayed.call(retry_task, 2).parsed.structured == {"verdict": "confirmed"}


def test_invalid_recording_sets_are_rejected(tmp_path):
    with pytest.raises(RecordingError):
        RecordingSet(tmp_path)
    (tmp_path / "index.json").write_text('{"recordings": []}', encoding="utf-8")
    with pytest.raises(RecordingError):
        RecordingSet(tmp_path)
    other = tmp_path / "other"
    record(other, task(), result={**RESULT, "status": "ok", "output": None})
    with pytest.raises(RecordingError):
        RecordingSet(other).find("claim-verifier", "P-0042", 1)


def test_patches_are_applied_to_the_workdir(tmp_path, repos):
    _, repo = repos.origin_and_clone()
    patch = ("diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
             "@@ -1 +1 @@\n-# demo\n+# demo changed by the agent\n")
    writer = task(workdir=repo, access=Access.WORKSPACE_WRITE, role="fix-executor",
                  subject=replace(task().subject, type="issue", id="0007"),
                  output_schema="runner/roles/fix-executor.schema.json")
    record(tmp_path / "set", writer, patch=patch)
    replay = ReplayAdapter(RecordingSet(tmp_path / "set"), VcsProcess(environ=repos.environ))
    assert replay.call(writer, 1).forced is None
    assert (repo / "README.md").read_text(encoding="utf-8") == "# demo changed by the agent\n"
    failed = replay.call(writer, 1)
    assert failed.forced == (RunnerStatus.FAILED, "tool-error")
    assert "无法应用" in failed.parsed.error_message
