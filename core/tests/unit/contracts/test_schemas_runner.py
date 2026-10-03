import pytest
from contract_samples import changed, paths, without

from tightrein.contracts import validate

TASK = "runner/runner-task.schema.json"
RESULT = "runner/runner-result.schema.json"
EVENT = "runner/transcript-event.schema.json"
GUARD = "runner/guard-report.schema.json"
REPLAY = "runner/replay-index.schema.json"
CHANGE = "runner/change-request.schema.json"
OPERATION = "data/pending-operation.schema.json"

RUNNER_TASK = {
    "runId": "R-20260929-021503-triage", "stage": "triage", "role": "claim-verifier",
    "subject": {"type": "problem", "id": "P-0042"}, "attempt": 1,
    "instructions": {"prompt": "判断以下主张是否成立", "skills": [{"name": "triage", "references": ["roles/claim-verifier.md"]}],
                     "context": [{"id": "DP-0012", "type": "defect-pattern", "summary": "分页参数未校验",
                                  "path": "knowledge/defect-pattern/DP-0012-unchecked-paging.md",
                                  "reason": "path:src/Services/ 前缀匹配"}]},
    "workdir": "/work/worktrees/readonly", "outputSchema": "runner/roles/claim-verifier.schema.json", "access": "read-only",
    "allowedCommands": ["git log", "git show"], "limits": {"maxTurns": 40, "maxDurationMs": 600000, "maxCostUsd": None},
    "interactive": False, "tool": None, "model": None, "capability": None, "approvedProtectedPaths": [], "web": False,
    "readPaths": [],
}
USAGE = {"inputTokens": 1200, "outputTokens": 300, "cachedInputTokens": None, "costUsd": 0.02, "costEstimated": True}
RUNNER_RESULT = {
    "status": "ok", "errorType": None, "output": {"verdict": "confirmed"}, "usage": USAGE, "durationMs": 81000,
    "attempts": 1, "tool": "claude", "model": "claude-opus", "sessionId": "5f0c", "transcriptPath":
    "transcripts/claim-verifier-P-0042.jsonl", "guardReport": "raw/guards/claim-verifier-P-0042.json", "violations": [],
}
VIOLATION = {"kind": "readonly-modified", "path": "src/a.cs", "detail": "只读 worktree 中的文件被修改"}
EVENT_SAMPLE = {
    "seq": 3, "timestamp": "2026-09-29T02:16:00Z", "runId": "R-20260929-021503-triage", "role": "claim-verifier",
    "subjectId": "P-0042", "attempt": 1, "tool": "claude", "model": None, "sessionId": "5f0c", "type": "tool-call",
    "actor": "assistant", "text": None, "toolName": "Read", "toolInput": {"file_path": "src/a.cs"}, "toolCallId": "t1",
    "toolOutput": None, "isError": None, "usage": None,
}
GUARD_REPORT = {"ok": False, "violations": [VIOLATION], "changedFiles": ["src/a.cs"], "linesAdded": 2, "linesRemoved": 0,
                "removedEnvNames": ["GH_TOKEN"]}
REPLAY_INDEX = {"recordings": [{"role": "claim-verifier", "subjectId": "P-0042", "attempt": 1,
                                "dir": "claim-verifier-P-0042.1", "taskSha256": "e" * 64}]}
CHANGE_REQUEST = {"kind": "pull-request", "subjectId": "0007", "reason": "验证通过后提 PR",
                  "arguments": {"title": "Fix order query 500"}}
OPERATION_SAMPLE = {
    "id": "OP-0016", "stage": "release", "subjectId": "0007", "kind": "commit", "executor": "vcs",
    "commands": [{"argv": ["git", "add", "--", "src/Services/OrderService.cs"], "cwd": "worktrees/fix-0007",
                  "description": "暂存改动"}],
    "description": {"repo": "worktrees/fix-0007", "branch": "cty/fix-order-query-500", "files": ["src/Services/OrderService.cs"],
                    "affectsRemote": False, "undo": "git reset --soft HEAD~1", "text": "提交修复"},
    "impact": "修复分支新增一个提交", "reversible": True, "preconditions": {"head": "d6f37025"},
    "idempotencyKey": "commit:0007:" + "a" * 64, "confirmationsRequired": 1, "confirmationsGiven": 0, "status": "pending",
    "createdAt": "2026-09-29T06:00:00Z", "decidedAt": None, "executedAt": None, "result": None,
}

VALID = [
    (TASK, RUNNER_TASK),
    (TASK, changed(RUNNER_TASK, interactive=True, outputSchema=None, role="fix-session")),
    (RESULT, RUNNER_RESULT),
    (RESULT, changed(RUNNER_RESULT, status="guard-violation", errorType="readonly-modified", output=None,
                     violations=[VIOLATION])),
    (RESULT, changed(RUNNER_RESULT, status="limit-reached", errorType="daily-budget", output=None, attempts=0,
                     sessionId=None, transcriptPath=None, guardReport=None)),
    (EVENT, EVENT_SAMPLE),
    (EVENT, {"seq": 1, "timestamp": "2026-09-29T02:15:10Z", "runId": "R-20260929-021503-triage", "role": "claim-verifier",
             "subjectId": "P-0042", "attempt": 1, "tool": "codex", "type": "message", "actor": "system", "text": "未知事件"}),
    (GUARD, GUARD_REPORT),
    (REPLAY, REPLAY_INDEX),
    (CHANGE, CHANGE_REQUEST),
    (OPERATION, OPERATION_SAMPLE),
    (OPERATION, changed(OPERATION_SAMPLE, kind="fix-plan", stage="fix", commands=[])),
    (OPERATION, changed(OPERATION_SAMPLE, kind="local-migration", stage="fix", subjectId="0003", commands=[])),
    (OPERATION, changed(OPERATION_SAMPLE, kind="cleanup", confirmationsRequired=2, confirmationsGiven=1)),
]

INVALID = [
    (TASK, changed(RUNNER_TASK, outputSchema=None), "$.outputSchema"),
    (TASK, changed(RUNNER_TASK, outputSchema="contracts/schemas/runner/roles/claim-verifier.json"), "$.outputSchema"),
    (TASK, changed(RUNNER_TASK, access="full"), "$.access"),
    (TASK, without(RUNNER_TASK, "readPaths"), "$"),
    (RESULT, changed(RUNNER_RESULT, output=None), "$.output"),
    (RESULT, changed(RUNNER_RESULT, status="failed", errorType="tool-error"), "$.output"),
    (RESULT, changed(RUNNER_RESULT, status="guard-violation", errorType="readonly-modified", output=None),
     "$.violations"),
    (RESULT, changed(RUNNER_RESULT, status="failed", errorType="crashed", output=None), "$.errorType"),
    (EVENT, changed(EVENT_SAMPLE, toolName=None), "$.toolName"),
    (EVENT, changed(EVENT_SAMPLE, type="thinking"), "$.type"),
    (EVENT, changed(EVENT_SAMPLE, type="tool-result", toolOutput="x" * 16385), "$.toolOutput"),
    (GUARD, changed(GUARD_REPORT, violations=[changed(VIOLATION, kind="wrote-file")]), "$.violations[0].kind"),
    (REPLAY, {"recordings": []}, "$.recordings"),
    (REPLAY, {"recordings": [changed(REPLAY_INDEX["recordings"][0], taskSha256="abc")]}, "$.recordings[0].taskSha256"),
    (CHANGE, changed(CHANGE_REQUEST, kind="force-push"), "$.kind"),
    (OPERATION, changed(OPERATION_SAMPLE, commands=[]), "$.commands"),
    (OPERATION, changed(OPERATION_SAMPLE, executor="user"), "$.executor"),
    (OPERATION, changed(OPERATION_SAMPLE, kind="cleanup"), "$.confirmationsRequired"),
    (OPERATION, changed(OPERATION_SAMPLE, commands=[{"argv": [], "cwd": ".", "description": "x"}]),
     "$.commands[0].argv"),
]


@pytest.mark.parametrize("name,instance", VALID)
def test_valid_samples(name, instance):
    assert paths(name, instance) == set()


@pytest.mark.parametrize("name,instance,path", INVALID)
def test_invalid_samples(name, instance, path):
    assert path in paths(name, instance)


def test_output_schema_of_a_task_names_an_existing_schema():
    assert RUNNER_TASK["outputSchema"] in validate.names()
