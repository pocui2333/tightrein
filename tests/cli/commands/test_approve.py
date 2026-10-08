from tightrein.cli import exit_codes
from tightrein.store.tables import issues


def issue(issue_id: str, gate: str | None = None) -> issues.Issue:
    return issues.Issue(id=issue_id, status="implementing", title="笔记自动保存", kind="bug", origin="problem",
                        gate=gate, step=gate)


def test_a_gate_decision_is_recorded_for_the_next_run_and_the_gate_is_cleared(cli):
    cli.save(issues, issue("0019", "implement.approve"))
    data = cli.json("approve", "19", "--option", "2", "--note", "防抖 2s", "--yes")
    assert data["exitCode"] == exit_codes.OK and data["next"] == "tightrein run --object 0019"
    saved = cli.load(issues, "0019")
    assert saved.gate is None
    assert saved.extra["decisions"] == [{"point": "implement.approve", "verdict": "approve", "option": 2,
                                         "note": "防抖 2s", "at": "2026-10-08T03:00:00Z"}]


def test_reject_needs_a_reason_and_only_waiting_issues_can_be_reviewed(cli):
    cli.save(issues, issue("0019", "implement.approve"))
    cli.save(issues, issue("0020"))
    assert cli("reject", "0019", "--yes").code == exit_codes.USAGE  # --note 必填
    assert cli("reject", "0019", "--note", "  ", "--yes").code == exit_codes.USAGE
    assert cli("approve", "0020", "--yes").code == exit_codes.USAGE  # 没在等审核
    assert cli("approve", "P-0001", "--yes").code == exit_codes.USAGE  # 只审 Issue
    assert cli("approve", "0042", "--yes").code == exit_codes.USAGE  # 不存在
    assert cli.load(issues, "0019").extra == {}
    assert cli.json("reject", "0019", "--note", "保存失败没处理", "--yes")["exitCode"] == exit_codes.OK
    assert cli.load(issues, "0019").extra["decisions"][0]["verdict"] == "reject"
