"""一次运行中各阶段共用的依赖。由 cli 组装(唯一接触真实外部依赖的地方)，测试整体替换。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace

from tightrein.agents.call import AgentContext
from tightrein.onboard.setup import Setup
from tightrein.protocol.git import Git, GitHub, WriteScope
from tightrein.protocol.limits import Breaker
from tightrein.protocol.naming import Clock
from tightrein.protocol.process import ProcessRunner
from tightrein.protocol.raw import RawDir, raw_dir
from tightrein.protocol.records import EventLog
from tightrein.protocol.resources import IssueBudget, Quota, Slots
from tightrein.protocol.security import Redactor
from tightrein.settings.load import Settings
from tightrein.store.db import connect
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout


@dataclass
class Runtime:
    tool: ToolLayout
    workspace: WorkspaceLayout
    settings: Settings
    setup: Setup
    conn: sqlite3.Connection
    clock: Clock
    runner: ProcessRunner
    redactor: Redactor
    secrets: Mapping[str, str]  # 两处 secrets.json 合并；只给程序用，永不交给 agent
    environ: Mapping[str, str]
    run: str
    events: EventLog
    agents: AgentContext
    git: Git  # 项目主仓库：只读用途；写操作在 worktree 上经 git.at(...)
    github: GitHub | None
    slots: Slots

    @property
    def language(self) -> str:
        return self.settings.project.language if self.settings.project else "zh"

    def scope(self, subject: str, point: str) -> WriteScope:
        """写操作的幂等范围；步骤登记了文件序号时带上这一步的原始输出目录，git、gh 命令的输出按步存在那里。"""
        try:
            raw: RawDir | None = RawDir(raw_dir(self.workspace, self.run, point))
        except ValueError:
            raw = None
        return WriteScope(conn=self.conn, clock=self.clock, subject=subject, point=point, raw=raw)


@contextmanager
def isolated(runtime: Runtime) -> Iterator[Runtime]:
    """在别的线程里用的 Runtime：SQLite 连接只能在打开它的线程里用，所以连接(连同调用模型时用到的熔断、额度、用量
    计数)在这个线程里另开，用完关闭；其余依赖照旧共用。采集并行跑来源(collect/collect.py)、静态巡检并行取证
    (collect/static/verify.py)都经它。"""
    conn = connect(runtime.workspace.database)
    try:
        clock, settings = runtime.clock, runtime.settings
        agents = replace(runtime.agents, conn=conn, breaker=Breaker(conn, clock, settings),
                         quota=Quota(conn, clock, settings), budget=IssueBudget(conn, clock, settings))
        yield replace(runtime, conn=conn, agents=agents)
    finally:
        conn.close()
