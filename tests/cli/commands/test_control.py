from datetime import UTC, datetime

from tightrein.cli import exit_codes
from tightrein.protocol import recovery
from tightrein.store.tables import issues, runs

NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)


def test_pause_and_resume(cli):
    assert cli.json("pause", "--note", "看一下日志", "--yes")["next"] == "tightrein resume"
    control = recovery.control(cli.layout)
    assert control.mode is recovery.Mode.PAUSED and control.note == "看一下日志"
    data = cli.json("resume", "--yes")
    assert data["result"] == {"previous": "paused"} and recovery.control(cli.layout) is None
    assert cli.json("resume", "--yes")["exitCode"] == exit_codes.OK  # 没有暂停：只说明，不报错


def test_stop_terminates_only_runs_of_this_host(cli):
    for run_id, pid, host in (("R-20261008T020000Z-implement", 111, cli.externals.host),
                              ("R-20261008T020100Z-collect", 222, "other-host")):
        conn = cli.conn()
        runs.start(conn, runs.Run(id=run_id, stage="run", trigger="schedule", status="running", started_at=NOW,
                                  holder_pid=pid, holder_host=host))
        conn.close()
    data = cli.json("stop", "--yes")
    assert data["result"] == {"stopped": ["R-20261008T020000Z-implement"]}
    assert cli.terminated == [111]
    assert recovery.control(cli.layout).mode is recovery.Mode.STOPPED


def test_take_and_give_go_through_the_issue_state_machine(cli, monkeypatch):
    from tightrein.assess.issue import transitions

    events = []
    monkeypatch.setattr(transitions, "apply_event",
                        lambda runtime, subject, event, **options: events.append((subject, event, options)))
    cli.save(issues, issues.Issue(id="0019", status="implementing", title="x", kind="bug", origin="problem"))
    assert cli.json("take", "19", "--yes")["next"] == "tightrein give 0019"
    assert cli.json("give", "0019", "--yes")["next"] == "tightrein run --object 0019"
    assert events == [("0019", transitions.IssueEvent.TAKE, {"reason": "alice"}),
                      ("0019", transitions.IssueEvent.GIVE, {"reason": None})]
    assert cli("take", "P-0001", "--yes").code == exit_codes.USAGE
