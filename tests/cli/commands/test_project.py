import json

from tightrein.cli import exit_codes
from tightrein.onboard import check as trial
from tightrein.protocol.process import Outcome
from tightrein.protocol.schedule import launchd
from tightrein.store.tables import state


def override(cli, overrides: dict) -> None:
    data = json.loads(cli.layout.settings.read_text(encoding="utf-8"))
    data["overrides"] = overrides
    cli.layout.settings.write_text(json.dumps(data), encoding="utf-8")


def test_list_and_config_explain_show_each_layer(cli):
    listed = cli.json("project", "list")
    assert [item["project"] for item in listed["result"]] == ["shop"]
    override(cli, {"schedule": {"tick": "30m"}})
    explained = cli.json("project", "config", "schedule.tick", "--explain")
    assert explained["result"]["layers"] == [{"layer": "defaults", "value": "15m"},
                                             {"layer": "workspace", "value": "30m"}]
    assert cli.json("project", "config", "schedule.tick")["result"]["value"] == "30m"


def test_ready_needs_a_passing_check_of_the_current_setup(cli):
    refused = cli.json("project", "ready", "--yes")
    assert refused["exitCode"] == exit_codes.FAILED and refused["next"] == "tightrein project check"
    conn = cli.conn()
    report = trial.Report("2026-10-08T02:00:00Z", trial.setup_hash(cli.layout.setup), [])
    state.put(conn, trial.STATE_KEY, report.to_json(), cli.externals.clock)
    conn.close()
    data = cli.json("project", "ready", "--yes")
    assert data["exitCode"] == exit_codes.OK and data["result"]["plist"] is None  # 不是 macOS：不装定时器
    conn = cli.conn()
    assert state.get(conn, trial.READY_KEY) == {"at": "2026-10-08T03:00:00Z"}
    conn.close()


def test_ready_on_macos_installs_the_timer_and_remove_takes_it_down(cli):
    cli.externals.platform = "darwin"
    conn = cli.conn()
    report = trial.Report("2026-10-08T02:00:00Z", trial.setup_hash(cli.layout.setup), [])
    state.put(conn, trial.STATE_KEY, report.to_json(), cli.externals.clock)
    conn.close()
    cli.runner.answers["launchctl"] = Outcome(0, "", "", 1, None, None)
    data = cli.json("project", "ready", "--yes")
    plist = launchd.plist_path(cli.externals.home, "shop")
    assert data["result"]["plist"] == str(plist) and plist.is_file()
    assert [command.argv[:2] for command in cli.runner.commands if command.argv[0] == "launchctl"] == [
        ("launchctl", "bootstrap")]
    assert cli("project", "remove", "shop").code == exit_codes.USAGE  # 要确认
    assert cli.json("project", "remove", "shop", "--yes")["exitCode"] == exit_codes.OK
    assert not plist.exists() and not cli.layout.root.exists()
