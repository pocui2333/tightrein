"""launchd 定时配置(architecture/09 第 8 节)。

plist 放在 ~/Library/LaunchAgents/local.tightrein.<工作区名>.plist，用户级 LaunchAgent，在登录的图形会话域中运行。
launchd 按 schedule.tick 唤醒 tightrein tick(固定时刻完整运行，其余时刻只检查事件，见 orchestrator/service.tick)。
StartCalendarInterval 由 schedule.tick 展开：每个星期、小时(给出时)与分钟的组合一个字典，省略的键视为通配；
launchd 的 Weekday 中 7 与 0 都是周日，与 tick.weekdays 的写法一致。launchd 不经过 shell、不展开 ~，路径一律绝对。
日志写到工作区 data/logs/launchd.out.log 与 launchd.err.log，由编排每天的保留期清理轮转。
"""

from __future__ import annotations

import plistlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from tightrein.store.files.layout import WorkspaceLayout
from tightrein.vcs.process import Completed

LAUNCHCTL = "launchctl"
PROCESS_TYPE = "Standard"

Run = Callable[[Sequence[str]], Completed]


class LaunchdError(Exception):
    """launchctl 命令失败；plist 文件保留，便于手动排查。"""


def label(plist_path: Path) -> str:
    return plist_path.name.removesuffix(".plist")


def intervals(tick: Mapping[str, Any]) -> list[dict[str, int]]:
    hours: Sequence[int | None] = tick.get("hours") or [None]
    return [{"Weekday": weekday, **({"Hour": hour} if hour is not None else {}), "Minute": minute}
            for weekday in tick["weekdays"] for hour in hours for minute in tick["minutes"]]


def build(plist_path: Path, layout: WorkspaceLayout, program: Path, tool_root: Path, tick: Mapping[str, Any],
          path_env: str, lang: str) -> dict[str, Any]:
    """plist 的内容；PATH 以 tightrein 所在目录开头，其后为调用方给出的 PATH(去掉重复)。"""
    directories = [str(program.parent), *[item for item in path_env.split(":") if item]]
    path = ":".join(dict.fromkeys(directories))
    return {
        "Label": label(plist_path),
        "ProgramArguments": [str(program), "tick", "--workspace", str(layout.root.resolve())],
        "StartCalendarInterval": intervals(tick),
        "WorkingDirectory": str(tool_root.resolve()),
        "EnvironmentVariables": {"PATH": path, "LANG": lang},
        "StandardOutPath": str(layout.launchd_out_log()),
        "StandardErrorPath": str(layout.launchd_err_log()),
        "RunAtLoad": False,
        "ProcessType": PROCESS_TYPE,
    }


def render(data: Mapping[str, Any]) -> bytes:
    return plistlib.dumps(dict(data), sort_keys=False)


class Launchd:
    """写入或删除 plist 并调用 launchctl；run 执行外部命令并返回结果(不因非零退出抛出)。"""

    def __init__(self, run: Run, uid: int) -> None:
        self.run = run
        self.uid = uid

    def domain(self) -> str:
        return f"gui/{self.uid}"

    def service(self, plist_path: Path) -> str:
        return f"{self.domain()}/{label(plist_path)}"

    def install_commands(self, plist_path: Path) -> list[list[str]]:
        """已存在时先 bootout 再 bootstrap，使修改后的 tick 生效。"""
        bootstrap = [LAUNCHCTL, "bootstrap", self.domain(), str(plist_path)]
        if plist_path.exists():
            return [[LAUNCHCTL, "bootout", self.service(plist_path)], bootstrap]
        return [bootstrap]

    def uninstall_commands(self, plist_path: Path) -> list[list[str]]:
        return [[LAUNCHCTL, "bootout", self.service(plist_path)]]

    def _execute(self, argv: Sequence[str]) -> Completed:
        completed = self.run(argv)
        if completed.returncode != 0:
            raise LaunchdError(f"{' '.join(argv)} 失败(退出码 {completed.returncode})：{completed.stderr.strip()}")
        return completed

    def install(self, plist_path: Path, content: bytes) -> list[list[str]]:
        commands = self.install_commands(plist_path)
        if len(commands) > 1:
            self._execute(commands[0])
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_bytes(content)
        self._execute(commands[-1])
        return commands

    def uninstall(self, plist_path: Path) -> list[list[str]]:
        commands = self.uninstall_commands(plist_path)
        for argv in commands:
            self._execute(argv)
        plist_path.unlink(missing_ok=True)
        return commands

    def show(self, plist_path: Path) -> str:
        """`launchctl print` 的输出；未加载时为错误输出。"""
        completed = self.run([LAUNCHCTL, "print", self.service(plist_path)])
        return completed.stdout if completed.returncode == 0 else completed.stderr
