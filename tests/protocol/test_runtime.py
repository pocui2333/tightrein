"""Runtime.scope：写操作的幂等范围带上这一步的原始输出目录(git、gh 命令的输出按步存在那里)；isolated：别的线程里用的
Runtime 在那个线程里另开连接。"""

import sqlite3
import threading
from dataclasses import replace

import pytest

from tightrein.agents.call import AgentContext
from tightrein.protocol.naming import SystemClock
from tightrein.protocol.raw import raw_dir
from tightrein.protocol.runtime import Runtime, isolated
from tightrein.store.files.layout import WorkspaceLayout

RUN = "R-20261008T030000Z-release"


def make_runtime(layout: WorkspaceLayout) -> Runtime:
    return Runtime(tool=None, workspace=layout, settings=None, setup=None, conn=None, clock=None,  # type: ignore[arg-type]
                   runner=None, redactor=None, secrets={}, environ={}, run=RUN, events=None,  # type: ignore[arg-type]
                   agents=None, git=None, github=None, slots=None)  # type: ignore[arg-type]


def test_a_registered_step_gets_its_raw_dir(tmp_path):
    layout = WorkspaceLayout(tmp_path)
    scope = make_runtime(layout).scope("0007", "release.pr")
    assert (scope.subject, scope.point) == ("0007", "release.pr")
    assert scope.raw is not None and scope.raw.root == raw_dir(layout, RUN, "release.pr")


def test_an_unregistered_step_has_no_raw_dir(tmp_path):
    assert make_runtime(WorkspaceLayout(tmp_path)).scope("0007", "unknown.step").raw is None


VALUES = {"limits.breaker.dependencyFailures": 3, "limits.breaker.objectFailures": 3,
          "resources.quota.reserveFiveHour": 0.1, "resources.quota.reserveWeekly": 0.1,
          "resources.issueTokens": 2_000_000, "resources.cacheReadWeight": 0.1}


class _Settings:
    def get(self, key):
        return VALUES[key]

    def duration(self, key):
        return 60.0


def test_isolated_opens_the_connection_in_the_calling_thread(tmp_path):
    """别的线程里用的 Runtime：连接连同熔断、额度、用量计数在那个线程里另开，用完关闭；原来的不动。"""
    layout = WorkspaceLayout(tmp_path)
    layout.data_dir.mkdir(parents=True)
    agents = AgentContext(settings=None, layout=layout, conn=None, clock=SystemClock(), runner=None,  # type: ignore[arg-type]
                          redactor=None, events=None, environ={}, breaker=None, quota=None,  # type: ignore[arg-type]
                          budget=None, tool=None)  # type: ignore[arg-type]
    runtime = replace(make_runtime(layout), settings=_Settings(), clock=SystemClock(), agents=agents)
    seen = {}

    def work():
        with isolated(runtime) as local:
            local.conn.execute("SELECT 1")  # 在打开它的线程里可用
            seen.update(conn=local.conn, agents=local.agents)
        seen["thread"] = threading.get_ident()

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    local = seen["agents"]
    assert seen["conn"] is local.conn is local.breaker.conn is local.quota.conn is local.budget.conn
    assert runtime.conn is None and runtime.agents.conn is None  # 原来的不动
    with pytest.raises(sqlite3.ProgrammingError):
        seen["conn"].execute("SELECT 1")  # 用完已关闭
