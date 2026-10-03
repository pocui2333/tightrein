from pathlib import Path

import pytest

from tightrein.contracts.validate import SchemaValidationError
from tightrein.domain.enums import Access, RunnerStatus, Stage, ViolationKind
from tightrein.guards.report import Violation
from tightrein.runner.result import RunnerResult, Usage
from tightrein.runner.task import ContextItem, Instructions, Limits, RunnerTask, SkillRef, Subject

TASK = RunnerTask(
    run_id="R-20261005-030000-triage", stage=Stage.TRIAGE, role="claim-verifier", subject=Subject("problem", "P-0042"),
    attempt=1,
    instructions=Instructions("判断以下主张是否成立", (SkillRef("triage", ("roles/claim-verifier.md",)),),
                              (ContextItem("DP-0012", "defect-pattern", "分页参数未校验",
                                           "knowledge/defect-pattern/DP-0012-paging.md", "路径前缀匹配"),)),
    workdir=Path("/ws/worktrees/readonly"), output_schema="runner/roles/claim-verifier.schema.json", access=Access.READ_ONLY,
    allowed_commands=("git log", "git show"), limits=Limits(max_turns=40),
)


def test_task_round_trip():
    data = TASK.to_dict()
    assert data["subject"] == {"type": "problem", "id": "P-0042"}
    assert data["limits"] == {"maxTurns": 40, "maxDurationMs": None, "maxCostUsd": None}
    assert data["instructions"]["skills"] == [{"name": "triage", "references": ["roles/claim-verifier.md"]}]
    assert RunnerTask.from_dict(data) == TASK
    assert (TASK.subject_id, TASK.readonly) == ("P-0042", True)


def test_invalid_tasks_are_rejected():
    with pytest.raises(SchemaValidationError):
        RunnerTask.from_dict({**TASK.to_dict(), "outputSchema": None})
    with pytest.raises(SchemaValidationError):
        TASK.with_limits(Limits(max_turns=0)).to_dict()


def test_limits_are_filled_from_defaults():
    assert Limits(max_turns=40).filled(Limits(10, 600000, 2.5)) == Limits(40, 600000, 2.5)
    assert Limits().filled(Limits()) == Limits()


def test_usage_addition():
    first = Usage(100, 20, None, None, False)
    second = Usage(50, 10, 30, 0.02, True)
    assert first + second == Usage(150, 30, 30, 0.02, True)
    assert Usage.total([]) == Usage()
    assert Usage.from_dict(second.to_dict()) == second


def test_result_round_trip_and_schema():
    result = RunnerResult(RunnerStatus.OK, "claude", "claude-opus", output={"verdict": "confirmed"},
                          usage=Usage(1200, 300, None, 0.02, True), duration_ms=81000, attempts=1, session_id="5f0c",
                          transcript_path="transcripts/claim-verifier-P-0042.jsonl",
                          guard_report="raw/guards/claim-verifier-P-0042.json")
    assert RunnerResult.from_dict(result.to_dict()) == result
    violation = Violation(ViolationKind.READONLY_MODIFIED, "src/A.cs", "只读 worktree 中的文件被修改")
    blocked = RunnerResult(RunnerStatus.GUARD_VIOLATION, "claude", error_type="readonly-modified",
                           violations=(violation,))
    assert RunnerResult.from_dict(blocked.to_dict()).violations == (violation,)
    with pytest.raises(SchemaValidationError):
        RunnerResult(RunnerStatus.OK, "claude").to_dict()
    with pytest.raises(SchemaValidationError):
        RunnerResult(RunnerStatus.FAILED, "claude", error_type="crashed").to_dict()
