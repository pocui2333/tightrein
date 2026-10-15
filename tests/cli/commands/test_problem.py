import json
from dataclasses import replace
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


def test_retriage_only_marks_the_problem_and_override_needs_a_verdict(cli):
    cli.save(problems, problem("P-0001", "new"))
    cli.save(problems, replace(cli.load(problems, "P-0001"), extra={"assess": {"manual": True}}))
    result = cli.json("problem", "retriage", "P-0001", "--note", "只在月底出现", "--yes")
    assert result["exitCode"] == 0 and result["next"] == "tightrein run assess --object P-0001"
    record = cli.load(problems, "P-0001").extra["assess"]
    assert record["retriage"] is True and record["manual"] is False and record["userNotes"] == ["只在月底出现"]
    cli.save(problems, problem("P-0002", "closed"))
    assert cli("problem", "retriage", "P-0002", "--yes").code == exit_codes.USAGE
    assert cli("problem", "override", "P-0001", "--verdict", "maybe", "--reason", "x", "--yes").code == \
        exit_codes.USAGE


def test_override_and_new_refuse_while_a_run_holds_the_run_lock(cli):
    """改判、提需求会评估、动只读 worktree：运行中直接拒绝，不和那一轮抢只读 worktree。"""
    cli.save(problems, problem("P-0001", "new"))
    holder = {"pid": 4242, "host": "other-host", "acquiredAt": "2026-10-08T03:00:00Z",
              "heartbeatAt": "2026-10-08T03:00:00Z"}
    cli.layout.run_lock.parent.mkdir(parents=True, exist_ok=True)
    cli.layout.run_lock.write_text(json.dumps(holder))
    refused = cli.json("problem", "override", "P-0001", "--verdict", "refuted", "--reason", "x", "--yes")
    assert refused["exitCode"] == exit_codes.USAGE and "正有一轮在运行" in json.dumps(refused, ensure_ascii=False)
    assert cli.load(problems, "P-0001").status == "new"
    assert cli("new", "加个导出按钮", "--yes").code == exit_codes.USAGE
