from datetime import UTC, datetime, timedelta

from tightrein.cli import exit_codes
from tightrein.store.tables import problems

NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)


def problem(problem_id: str, status: str) -> problems.Problem:
    return problems.Problem(id=problem_id, fingerprint=problem_id, source="collect.alerts", check_type="alert",
                            status=status, title="出错", first_seen=NOW, last_seen=NOW)


def test_mute_unmute_and_reopen_record_who_why_and_when(cli):
    cli.save(problems, problem("P-0001", "new"))
    assert cli("problem", "mute", "P-0001", "--days", "0", "--note", "x", "--yes").code == exit_codes.USAGE
    assert cli.json("problem", "mute", "P-0001", "--days", "7", "--note", "已知问题", "--yes")["exitCode"] == 0
    muted = cli.load(problems, "P-0001")
    assert muted.status == "muted" and muted.muted_until == NOW + timedelta(days=7)
    assert muted.extra["ignore"]["until"] == "2026-10-15T03:00:00Z"
    assert muted.extra["manual"] == {"action": "mute", "note": "已知问题", "at": "2026-10-08T03:00:00Z",
                                     "by": "alice"}
    assert cli("problem", "mute", "P-0001", "--days", "7", "--note", "再一次", "--yes").code == exit_codes.USAGE
    cli("problem", "unmute", "P-0001", "--yes")
    unmuted = cli.load(problems, "P-0001")
    assert (unmuted.status, unmuted.muted_until) == ("new", None) and "ignore" not in unmuted.extra


def test_only_closed_or_resolved_problems_are_reopened_and_list_filters_by_status(cli):
    cli.save(problems, problem("P-0001", "new"))
    cli.save(problems, problem("P-0002", "closed"))
    assert cli("problem", "reopen", "P-0001", "--yes").code == exit_codes.USAGE
    assert cli("problem", "reopen", "0019", "--yes").code == exit_codes.USAGE  # 不是问题编号
    cli("problem", "reopen", "P-0002", "--note", "又出现了", "--yes")
    assert cli.load(problems, "P-0002").status == "new"
    listed = cli.json("problem", "list", "--status", "new")
    assert [item["id"] for item in listed["result"]] == ["P-0001", "P-0002"]
