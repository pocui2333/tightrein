import plistlib

import pytest

from tightrein.packaging import launchd
from tightrein.store.files.layout import UserLayout, WorkspaceLayout
from tightrein.vcs.process import Completed

TICK = {"weekdays": [1, 5], "minutes": [0, 30]}


class Launchctl:
    def __init__(self, failing=()):
        self.calls = []
        self.failing = set(failing)

    def __call__(self, argv):
        self.calls.append(list(argv))
        code = 5 if argv[1] in self.failing else 0
        return Completed(tuple(argv), code, "state = running\n" if code == 0 else "", "" if code == 0 else "拒绝")


def plist_path(tmp_path):
    return UserLayout(tmp_path / "home").launch_agent("demo")


def test_the_tick_expands_to_calendar_intervals():
    assert launchd.intervals(TICK) == [{"Weekday": 1, "Minute": 0}, {"Weekday": 1, "Minute": 30},
                                       {"Weekday": 5, "Minute": 0}, {"Weekday": 5, "Minute": 30}]
    assert launchd.intervals({"weekdays": [2], "hours": [8, 20], "minutes": [15]}) == [
        {"Weekday": 2, "Hour": 8, "Minute": 15}, {"Weekday": 2, "Hour": 20, "Minute": 15}]


def test_the_plist_uses_absolute_paths_and_workspace_logs(tmp_path):
    layout = WorkspaceLayout(tmp_path / "workspaces" / "demo")
    program = tmp_path / "venv" / "bin" / "tightrein"
    path = plist_path(tmp_path)
    data = launchd.build(path, layout, program, tmp_path / "tool", TICK, "/usr/bin:/opt/tools:/usr/bin", "en_US.UTF-8")
    assert path.name == "local.tightrein.demo.plist"
    assert data["Label"] == "local.tightrein.demo"
    assert data["ProgramArguments"] == [str(program), "tick", "--workspace", str(layout.root.resolve())]
    assert data["StartCalendarInterval"] == launchd.intervals(TICK)
    assert data["EnvironmentVariables"] == {"PATH": f"{program.parent}:/usr/bin:/opt/tools", "LANG": "en_US.UTF-8"}
    assert (data["StandardOutPath"], data["StandardErrorPath"]) == (str(layout.launchd_out_log()),
                                                                  str(layout.launchd_err_log()))
    assert (data["RunAtLoad"], data["ProcessType"]) == (False, "Standard")
    assert data["WorkingDirectory"] == str((tmp_path / "tool").resolve())
    assert plistlib.loads(launchd.render(data)) == data


def test_install_writes_the_file_then_bootstraps_and_reinstall_boots_out_first(tmp_path):
    path = plist_path(tmp_path)
    run = Launchctl()
    agent = launchd.Launchd(run, 501)
    agent.install(path, b"<plist/>")
    assert path.read_bytes() == b"<plist/>"
    assert run.calls == [["launchctl", "bootstrap", "gui/501", str(path)]]
    agent.install(path, b"<plist version='2'/>")
    assert run.calls[1:] == [["launchctl", "bootout", "gui/501/local.tightrein.demo"],
                             ["launchctl", "bootstrap", "gui/501", str(path)]]


def test_a_failing_launchctl_keeps_the_plist(tmp_path):
    path = plist_path(tmp_path)
    agent = launchd.Launchd(Launchctl(failing={"bootstrap"}), 501)
    with pytest.raises(launchd.LaunchdError, match="launchctl bootstrap gui/501 .* 失败\\(退出码 5\\)：拒绝"):
        agent.install(path, b"<plist/>")
    assert path.is_file()


def test_uninstall_boots_out_then_deletes_and_show_prints_the_state(tmp_path):
    path = plist_path(tmp_path)
    run = Launchctl()
    agent = launchd.Launchd(run, 501)
    agent.install(path, b"<plist/>")
    assert agent.show(path) == "state = running\n"
    agent.uninstall(path)
    assert not path.exists()
    assert run.calls[1:] == [["launchctl", "print", "gui/501/local.tightrein.demo"],
                             ["launchctl", "bootout", "gui/501/local.tightrein.demo"]]
