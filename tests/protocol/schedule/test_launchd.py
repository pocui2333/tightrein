import plistlib
from pathlib import Path

import pytest

from tightrein.protocol.process import Command, Outcome
from tightrein.protocol.schedule import launchd
from tightrein.store.files.layout import WorkspaceLayout


class Launchctl:
    """假的 launchctl：记下每条命令；failing 中的子命令返回退出码 5，missing 中的返回 113(服务没加载)。"""

    def __init__(self, failing: tuple[str, ...] = (), missing: tuple[str, ...] = ()) -> None:
        self.calls: list[list[str]] = []
        self.failing = set(failing)
        self.missing = set(missing)

    def run(self, command: Command) -> Outcome:
        self.calls.append(list(command.argv))
        verb = command.argv[1]
        code = 5 if verb in self.failing else 113 if verb in self.missing else 0
        return Outcome(code, "state = running\n" if code == 0 else "", "" if code == 0 else "拒绝", 1, None, None)


def make_job(tmp_path: Path, interval_s: float = 900) -> tuple[launchd.Job, WorkspaceLayout, Path]:
    layout = WorkspaceLayout(tmp_path / "workspaces" / "demo")
    program = tmp_path / "venv" / "bin" / "tightrein"
    job = launchd.build(workspace=layout, home=tmp_path / "home", program=program, tool_root=tmp_path / "tool",
                        interval_s=interval_s, path_env="/usr/bin:/opt/tools:/usr/bin", lang="en_US.UTF-8")
    return job, layout, program


def test_the_plist_uses_absolute_paths_and_workspace_logs(tmp_path: Path) -> None:
    job, layout, program = make_job(tmp_path)
    content = job.content
    assert job.plist == tmp_path / "home" / "Library" / "LaunchAgents" / "local.tightrein.demo.plist"
    assert content["Label"] == job.label == "local.tightrein.demo"
    assert content["ProgramArguments"] == [str(program), "run", "--trigger", "schedule", "--project", "demo"]
    assert content["EnvironmentVariables"] == {"PATH": f"{program.parent}:/usr/bin:/opt/tools",
                                               "LANG": "en_US.UTF-8", "TIGHTREIN_HOME": str(tmp_path / "tool")}
    out_log, err_log = launchd.log_paths(layout)
    assert (content["StandardOutPath"], content["StandardErrorPath"]) == (str(out_log), str(err_log))
    assert out_log.parent == layout.data_dir / "logs"
    assert content["WorkingDirectory"] == str(tmp_path / "tool")
    assert all(Path(value).is_absolute() for value in (content["WorkingDirectory"], content["StandardOutPath"],
                                                        *content["ProgramArguments"][:1]))
    assert (content["RunAtLoad"], content["ProcessType"]) == (False, "Background")
    assert plistlib.loads(launchd.render(content)) == content


def test_intervals_that_divide_an_hour_or_a_day_are_aligned_to_midnight() -> None:
    assert launchd.timing(900) == {"StartCalendarInterval": [{"Minute": minute} for minute in (0, 15, 30, 45)]}
    assert launchd.timing(6 * 3600) == {"StartCalendarInterval": [{"Hour": hour, "Minute": 0}
                                                                  for hour in (0, 6, 12, 18)]}
    assert launchd.timing(7 * 60) == {"StartInterval": 420}
    assert launchd.timing(5 * 3600) == {"StartInterval": 18000}


def test_install_writes_the_file_then_bootstraps_and_reinstall_boots_out_first(tmp_path: Path) -> None:
    job, _, _ = make_job(tmp_path)
    launchctl = Launchctl()
    agent = launchd.Launchd(launchctl, 501, tmp_path)
    agent.install(job)
    assert plistlib.loads(job.plist.read_bytes()) == job.content
    assert launchctl.calls == [["launchctl", "bootstrap", "gui/501", str(job.plist)]]
    assert Path(job.content["StandardOutPath"]).parent.is_dir()
    agent.install(job)
    assert launchctl.calls[1:] == [["launchctl", "bootout", "gui/501/local.tightrein.demo"],
                                   ["launchctl", "bootstrap", "gui/501", str(job.plist)]]


def test_a_bootout_of_a_service_that_is_not_loaded_is_not_a_failure(tmp_path: Path) -> None:
    job, _, _ = make_job(tmp_path)
    launchd.Launchd(Launchctl(), 501, tmp_path).install(job)
    launchctl = Launchctl(missing=("bootout",))
    launchd.Launchd(launchctl, 501, tmp_path).install(job)
    assert [call[1] for call in launchctl.calls] == ["bootout", "bootstrap"]


def test_a_failing_launchctl_keeps_the_plist(tmp_path: Path) -> None:
    job, _, _ = make_job(tmp_path)
    agent = launchd.Launchd(Launchctl(failing=("bootstrap",)), 501, tmp_path)
    with pytest.raises(launchd.LaunchdError, match=r"launchctl bootstrap gui/501 .* 失败\(退出码 5\)：拒绝"):
        agent.install(job)
    assert job.plist.is_file()


def test_uninstall_boots_out_then_deletes_and_show_prints_the_state(tmp_path: Path) -> None:
    job, _, _ = make_job(tmp_path)
    launchctl = Launchctl()
    agent = launchd.Launchd(launchctl, 501, tmp_path)
    agent.install(job)
    assert agent.show(job.label) == "state = running\n"
    agent.uninstall(job.label, job.plist)
    assert not job.plist.exists()
    assert launchctl.calls[1:] == [["launchctl", "print", "gui/501/local.tightrein.demo"],
                                   ["launchctl", "bootout", "gui/501/local.tightrein.demo"]]
    assert agent.uninstall(job.label, job.plist) == []  # 没装过：不调用 launchctl


def test_logs_are_rotated_by_size_and_keep_a_fixed_number(tmp_path: Path) -> None:
    log = tmp_path / "launchd.out.log"
    log.write_text("small")
    assert launchd.rotate(log, max_bytes=10, keep=2) is False
    for content in ("first-big-log", "second-big-log", "third-big-log"):
        log.write_text(content)
        assert launchd.rotate(log, max_bytes=10, keep=2) is True
    assert not log.exists()
    assert (tmp_path / "launchd.out.log.1").read_text() == "third-big-log"
    assert (tmp_path / "launchd.out.log.2").read_text() == "second-big-log"
    assert not (tmp_path / "launchd.out.log.3").exists()


def test_rotation_handles_gaps_in_the_backup_numbers(tmp_path: Path) -> None:
    log = tmp_path / "launchd.out.log"
    (tmp_path / "launchd.out.log.2").write_text("old-2")  # .1 缺失
    (tmp_path / "launchd.out.log.4").write_text("old-4")  # .3 缺失
    log.write_text("current-big-log")
    assert launchd.rotate(log, max_bytes=10, keep=4) is True
    backups = {path.name: path.read_text() for path in tmp_path.iterdir()}
    assert backups == {"launchd.out.log.1": "current-big-log", "launchd.out.log.3": "old-2",
                       "launchd.out.log.4": "old-4"}
