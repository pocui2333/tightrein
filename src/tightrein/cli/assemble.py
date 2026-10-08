"""组装根：全仓库唯一接触真实外部依赖(环境变量、子进程、时钟、进程号、家目录、终端)的地方。

命令只经 Externals 拿这些依赖，测试整体换成假实现；Runtime(protocol/runtime.py)在这里拼好交给调度与各阶段。
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import sqlite3
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from tightrein.agents.call import AgentContext, default_adapters
from tightrein.agents.tools.replay import ReplayAdapter
from tightrein.onboard import setup as setup_file
from tightrein.protocol.git import Git, GitError, GitHub, repo_slug
from tightrein.protocol.limits import Breaker
from tightrein.protocol.naming import Clock, SystemClock
from tightrein.protocol.process import ProcessRunner, SubprocessRunner
from tightrein.protocol.records import EventLog
from tightrein.protocol.resources import IssueBudget, Quota, Slots
from tightrein.protocol.runtime import Runtime
from tightrein.protocol.security import Redactor, child_env, load_secrets
from tightrein.settings.load import Settings
from tightrein.store.db import open_database
from tightrein.store.files.json import read_json
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout
from tightrein.store.locks import process_alive
from tightrein.store.tables import runs

PROGRAM = "tightrein"


class ProjectUnknown(LookupError):
    """没有给出项目，且从当前目录与工作区列表推断不出来。"""


@dataclass
class Externals:
    environ: Mapping[str, str]
    runner: ProcessRunner
    clock: Clock
    tool: ToolLayout
    home: Path
    cwd: Path
    program: Path  # tightrein 命令本身(launchd 与命令链接指向它)
    pid: int
    host: str
    uid: int
    stdin_is_tty: Callable[[], bool]
    alive: Callable[[int], bool]
    terminate: Callable[[int], None]  # 急停时给进行中运行的进程发 SIGTERM
    replay: ReplayAdapter | None = None  # 给出时全部模型调用回放录制(整体测试)
    platform: str = field(default=sys.platform)

    @classmethod
    def real(cls) -> Externals:
        program = Path(shutil.which(PROGRAM) or sys.argv[0]).absolute()
        return cls(
            environ=dict(os.environ), runner=SubprocessRunner(), clock=SystemClock(), tool=ToolLayout.discover(),
            home=Path.home(), cwd=Path.cwd(), program=program, pid=os.getpid(), host=socket.gethostname(),
            uid=os.getuid(), stdin_is_tty=sys.stdin.isatty, alive=process_alive,
            terminate=lambda pid: os.kill(pid, signal.SIGTERM),
        )


@dataclass
class Workspace:
    """一个项目的工作区：路径、合并后的配置、数据库连接。"""

    layout: WorkspaceLayout
    settings: Settings
    conn: sqlite3.Connection


def projects(tool: ToolLayout) -> list[str]:
    root = tool.workspaces_dir
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir() and (path / "settings.json").is_file())


def resolve_project(externals: Externals, given: str | None) -> str:
    """-p 优先；否则当前目录所在的工作区，或当前目录所在的项目仓库；只有一个项目时取它。"""
    found = projects(externals.tool)
    if given is not None:
        if given not in found:
            raise ProjectUnknown(f"没有项目 {given}；已有：{'、'.join(found) or '无'}")
        return given
    cwd = externals.cwd.resolve()
    for name in found:
        workspace = externals.tool.workspace(name)
        if cwd.is_relative_to(workspace.root.resolve()) or _inside_repo(workspace, cwd):
            return name
    if len(found) == 1:
        return found[0]
    if not found:
        raise ProjectUnknown("还没有接入项目：先执行 tightrein project add <仓库路径>")
    raise ProjectUnknown(f"有多个项目，用 -p 指定：{'、'.join(found)}")


def open_workspace(externals: Externals, project: str) -> Workspace:
    layout = externals.tool.workspace(project)
    settings = Settings.load(externals.tool, layout)
    return Workspace(layout, settings, open_database(layout.database, clock=externals.clock))


def new_run(workspace: Workspace, externals: Externals, stage: str) -> str:
    return runs.free_id(workspace.conn, externals.clock.now(), stage)


def runtime(externals: Externals, workspace: Workspace, run: str) -> Runtime:
    """拼一次运行的全部依赖；接入清单不合格、配置缺仓库路径时在这里报出。"""
    settings, layout, conn, clock = workspace.settings, workspace.layout, workspace.conn, externals.clock
    if settings.project is None or settings.project.repo is None:
        raise ValueError(f"{layout.settings}：project.repo 未填")
    setup = setup_file.load(layout)
    redactor = Redactor()
    secrets = {**load_secrets(externals.tool.secrets, redactor), **load_secrets(layout.secrets, redactor)}
    events = EventLog(layout.events(run), redactor, clock)
    slots = Slots(settings)
    agents = AgentContext(
        settings=settings, layout=layout, conn=conn, clock=clock, runner=externals.runner, redactor=redactor,
        events=events, environ=externals.environ, breaker=Breaker(conn, clock, settings),
        quota=Quota(conn, clock, settings), budget=IssueBudget(conn, clock, settings), slots=slots,
        replay=externals.replay, adapters=default_adapters(), tool=externals.tool,
    )
    env = child_env(externals.environ)
    git = Git(settings.project.repo, externals.runner, env, settings, redactor=redactor)
    return Runtime(
        tool=externals.tool, workspace=layout, settings=settings, setup=setup, conn=conn, clock=clock,
        runner=externals.runner, redactor=redactor, secrets=secrets, environ=externals.environ, run=run,
        events=events, agents=agents, git=git, github=_github(git, externals, env, settings, redactor), slots=slots,
    )


def _github(git: Git, externals: Externals, env: Mapping[str, str], settings: Settings,
            redactor: Redactor) -> GitHub | None:
    try:
        url = git.remote_url()
    except GitError:
        return None
    slug = repo_slug(url) if url else None
    if slug is None:
        return None
    return GitHub(git.repo, slug, externals.runner, env, settings, redactor=redactor)


def _inside_repo(workspace: WorkspaceLayout, cwd: Path) -> bool:
    try:
        repo = read_json(workspace.settings)["project"]["repo"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return bool(repo) and cwd.is_relative_to((workspace.root / repo).resolve())
