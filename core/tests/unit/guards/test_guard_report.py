import json

import pytest

from tightrein.contracts.validate import SchemaValidationError
from tightrein.domain.enums import ViolationKind
from tightrein.guards.report import GuardBlocked, GuardReport, Violation, summary, write_report

READONLY = Violation(ViolationKind.READONLY_MODIFIED, "src/A.cs", "只读 worktree 中的文件被修改")
HARDCODE = Violation(ViolationKind.SUSPECTED_HARDCODE, "src/A.cs", "新增字面量 \"ORD-20260929\" 与复现输入相同")


def test_report_follows_the_schema():
    report = GuardReport((READONLY,), ("src/A.cs",), 2, 1, ("GH_TOKEN",))
    assert report.to_dict() == {
        "ok": False, "violations": [{"kind": "readonly-modified", "path": "src/A.cs", "detail": "只读 worktree 中的文件被修改"}],
        "changedFiles": ["src/A.cs"], "linesAdded": 2, "linesRemoved": 1, "removedEnvNames": ["GH_TOKEN"],
    }
    assert GuardReport().to_dict() == {"ok": True, "violations": [], "changedFiles": [], "linesAdded": 0,
                                       "linesRemoved": 0}


def test_suspected_hardcode_does_not_fail_the_check():
    assert GuardReport((HARDCODE,)).ok
    report = GuardReport((HARDCODE, READONLY))
    assert not report.ok
    assert report.first_blocking == READONLY


def test_invalid_report_is_rejected():
    with pytest.raises(SchemaValidationError):
        GuardReport(lines_added=-1).to_dict()


def test_violation_round_trip_and_text():
    assert Violation.from_dict(READONLY.to_dict()) == READONLY
    assert str(Violation(ViolationKind.GIT_REMOTE_CHANGED, None, "origin 被修改")) == "git-remote-changed：origin 被修改"
    assert summary([READONLY, HARDCODE]).startswith("readonly-modified src/A.cs：")


def test_guard_blocked_needs_violations():
    blocked = GuardBlocked([Violation(ViolationKind.CREDENTIAL_PRESENT, ".env", "worktree 中有未跟踪的凭证文件")])
    assert "credential-present .env" in str(blocked)
    with pytest.raises(ValueError):
        GuardBlocked([])


def test_report_file_keeps_extra_details(tmp_path):
    path = tmp_path / "raw" / "guards" / "fix-executor-0007.json"
    write_report(path, GuardReport((READONLY,), restore_error="PermissionError"), {"snapshot": {"head": "abc"}})
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["report"]["ok"] is False
    assert document["restoreError"] == "PermissionError"
    assert document["snapshot"] == {"head": "abc"}
