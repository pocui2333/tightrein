"""macOS 的定时器(schedule.md「launchd」)：每个项目一个用户级 LaunchAgent，按 schedule.tick(从零点对齐)醒来执行
`tightrein run --trigger schedule -p <项目>`；到点判断在 tightrein 自己(state 表)，间隔以外的改动不用重装。

- plist 中路径一律绝对：launchd 不经 shell、不展开 `~`；
- PATH 以 tightrein 所在目录开头，其后接调用方的 PATH 并去重；显式设 LANG(launchd 的环境里没有)；
- 用户级 `gui/<uid>` 域；已存在时先 bootout 再 bootstrap，修改才生效；
- launchctl 失败时保留 plist 便于排查；
- 日志写进工作区 `data/logs/`，每次定时运行开始时按大小改名轮转(`.1`、`.2`…)。
"""

from __future__ import annotations

import plistlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.protocol.process import Command, ProcessRunner
from tightrein.store.files.atomic import write_bytes
from tightrein.store.files.layout import WorkspaceLayout

LAUNCHCTL = "launchctl"
LABEL_PREFIX = "local.tightrein."
AGENTS_DIR = Path("Library") / "LaunchAgents"
LOGS_DIR = "logs"
OUT_LOG = "launchd.out.log"
ERR_LOG = "launchd.err.log"
PROCESS_TYPE = "Background"
LAUNCHCTL_TIMEOUT_S = 30.0
MINUTE, HOUR, DAY = 60, 3600, 86400
# bootout 一个没加载的服务：macOS 版本不同返回 3(No such process)或 113(Could not find service)
NOT_LOADED = frozenset({3, 113})


class LaunchdError(Exception):
    """launchctl 失败；plist 保留，便于手动排查。"""


@dataclass(frozen=True)
class Job:
    label: str
    plist: Path
    content: dict[str, Any]


def label(project: str) -> str:
    return LABEL_PREFIX + project


def plist_path(home: Path, project: str) -> Path:
    return home / AGENTS_DIR / f"{label(project)}.plist"


def log_paths(workspace: WorkspaceLayout) -> tuple[Path, Path]:
    """launchd 的标准输出与标准错误(layout 中还没有这两个路径，先在这里算)。"""
    directory = workspace.data_dir / LOGS_DIR
    return directory / OUT_LOG, directory / ERR_LOG


def build(*, workspace: WorkspaceLayout, home: Path, program: Path, tool_root: Path, interval_s: float,
          path_env: str, lang: str) -> Job:
    program = program.absolute()
    directories = [str(program.parent), *(item for item in path_env.split(":") if item)]
    out_log, err_log = log_paths(workspace)
    project = workspace.project
    content = {
        "Label": label(project),
        "ProgramArguments": [str(program), "run", "--trigger", "schedule", "--project", project],
        **timing(interval_s),
        "WorkingDirectory": str(tool_root.absolute()),
        "EnvironmentVariables": {"PATH": ":".join(dict.fromkeys(directories)), "LANG": lang,
                                 "TIGHTREIN_HOME": str(tool_root.absolute())},
        "StandardOutPath": str(out_log.absolute()),
        "StandardErrorPath": str(err_log.absolute()),
        "RunAtLoad": False,
        "ProcessType": PROCESS_TYPE,
    }
    return Job(label(project), plist_path(home, project), content)


def timing(interval_s: float) -> dict[str, Any]:
    """醒来时刻：能整除一小时或一天的间隔写成 StartCalendarInterval(从零点对齐，status 推算的下次时刻与实际一致；
    机器睡眠错过的时刻 launchd 醒来后只补一次)；其余写 StartInterval。"""
    seconds = int(interval_s)
    if seconds < HOUR and seconds % MINUTE == 0 and HOUR % seconds == 0:
        return {"StartCalendarInterval": [{"Minute": minute} for minute in range(0, 60, seconds // MINUTE)]}
    if seconds % HOUR == 0 and DAY % seconds == 0:
        return {"StartCalendarInterval": [{"Hour": hour, "Minute": 0} for hour in range(0, 24, seconds // HOUR)]}
    return {"StartInterval": seconds}


def render(content: Mapping[str, Any]) -> bytes:
    return plistlib.dumps(dict(content), sort_keys=False)


def rotate(path: Path, *, max_bytes: int, keep: int) -> bool:
    """超过 max_bytes 时改名为 `.1`，原有的依次后移，最多留 keep 份；返回是否轮转了。"""
    if not path.is_file() or path.stat().st_size <= max_bytes:
        return False
    for number in range(keep - 1, 0, -1):
        older = path.with_name(f"{path.name}.{number}")
        if older.exists():
            older.replace(path.with_name(f"{path.name}.{number + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))
    return True


class Launchd:
    def __init__(self, runner: ProcessRunner, uid: int, cwd: Path) -> None:
        self.runner = runner
        self.uid = uid
        self.cwd = cwd

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    def service(self, job_label: str) -> str:
        return f"{self.domain}/{job_label}"

    def install_commands(self, job: Job) -> list[tuple[str, ...]]:
        """已存在时先 bootout 再 bootstrap，修改后的 plist 才生效。"""
        bootstrap = (LAUNCHCTL, "bootstrap", self.domain, str(job.plist))
        if job.plist.exists():
            return [(LAUNCHCTL, "bootout", self.service(job.label)), bootstrap]
        return [bootstrap]

    def uninstall_commands(self, job_label: str) -> list[tuple[str, ...]]:
        return [(LAUNCHCTL, "bootout", self.service(job_label))]

    def install(self, job: Job) -> list[tuple[str, ...]]:
        commands = self.install_commands(job)
        if len(commands) > 1:
            self._launchctl(commands[0], tolerate_missing=True)
        write_bytes(job.plist, render(job.content))
        for directory in {Path(job.content["StandardOutPath"]).parent, Path(job.content["StandardErrorPath"]).parent}:
            directory.mkdir(parents=True, exist_ok=True)
        self._launchctl(commands[-1])
        return commands

    def uninstall(self, job_label: str, plist: Path) -> list[tuple[str, ...]]:
        commands = self.uninstall_commands(job_label) if plist.exists() else []
        for argv in commands:
            self._launchctl(argv, tolerate_missing=True)
        plist.unlink(missing_ok=True)
        return commands

    def show(self, job_label: str) -> str:
        outcome = self.runner.run(Command((LAUNCHCTL, "print", self.service(job_label)), self.cwd, {},
                                          timeout_s=LAUNCHCTL_TIMEOUT_S))
        return outcome.stdout if outcome.exit_code == 0 else outcome.stderr_tail

    def _launchctl(self, argv: tuple[str, ...], *, tolerate_missing: bool = False) -> None:
        outcome = self.runner.run(Command(argv, self.cwd, {}, timeout_s=LAUNCHCTL_TIMEOUT_S))
        if outcome.exit_code == 0:
            return
        if tolerate_missing and outcome.exit_code in NOT_LOADED:  # 要卸的本来就没加载，不算失败
            return
        reason = outcome.start_error or outcome.stopped_by or f"退出码 {outcome.exit_code}"
        raise LaunchdError(f"{' '.join(argv)} 失败({reason})：{outcome.stderr_tail.strip()}")

