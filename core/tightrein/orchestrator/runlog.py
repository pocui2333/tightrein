"""loop 运行记录(architecture/09 3.6)。

每次 run 与 continue 产生一条 stage=loop 的运行记录 R-<日期>-<时分秒>-loop，运行期间以本运行编号为对象持有一把
对象锁：中断恢复与健康检查据锁的持有进程判断它是否仍在运行。各模块自己创建运行记录，编排在每一步
前记下已有的运行编号，步骤结束后把新出现的运行的 parent_run_id 指向本次 loop 运行(trace 不改写)。
编排层自身的判断以 operation=gate 写入事件日志：decision 为「执行」或「跳过」，reason 为触发条件的结果。
loop 记录的状态：任一子运行或步骤失败为 failed；否则有等待用户的事项为 blocked；否则为 ok。
运行编号精确到秒：Pacer 保证同一类调用(同一环节，collect 另按探针)不在同一秒开始，否则后一个运行会覆盖前一个。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from tightrein.domain.clock import Clock
from tightrein.domain.enums import RunStage, RunStatus
from tightrein.domain.run import Run
from tightrein.observability.events import EventLog
from tightrein.observability.tracing import Tracer
from tightrein.store import locks
from tightrein.store.repos import runs

EXECUTE = "执行"
SKIP = "跳过"


@dataclass
class LoopRun:
    run: Run
    tracer: Tracer

    @property
    def id(self) -> str:
        return self.run.id


def begin(conn: sqlite3.Connection, clock: Clock, events: EventLog, ttl: timedelta) -> LoopRun:
    started = clock.now()
    run_id = runs.free_id(conn, started, RunStage.LOOP)
    tracer = Tracer(events, clock, run_id=run_id, stage=RunStage.LOOP.value)
    run = Run(run_id, RunStage.LOOP, started, RunStatus.RUNNING, trace_id=tracer.trace_id)
    runs.save(conn, run)
    locks.acquire(conn, run_id, clock, ttl, run_id=run_id)
    return LoopRun(run, tracer)


def known_runs(conn: sqlite3.Connection) -> set[str]:
    return {run.id for run in runs.find(conn)}


def adopt(conn: sqlite3.Connection, loop: LoopRun, before: set[str]) -> list[Run]:
    """把 before 之后新出现的非 loop 运行关联到本次 loop 运行，返回它们。"""
    adopted = []
    for run in runs.find(conn):
        if run.id in before or run.stage is RunStage.LOOP or run.parent_run_id is not None:
            continue
        linked = replace(run, parent_run_id=loop.id)
        runs.save(conn, linked)
        adopted.append(linked)
    return adopted


def gate(loop: LoopRun, step: str, executed: bool, reason: str) -> None:
    loop.tracer.event("gate", decision=EXECUTE if executed else SKIP, reason=reason, attributes={"step": step})


def final_status(*, failed: bool, waiting: bool) -> RunStatus:
    if failed:
        return RunStatus.FAILED
    return RunStatus.BLOCKED if waiting else RunStatus.OK


def finish(conn: sqlite3.Connection, clock: Clock, loop: LoopRun, status: RunStatus) -> Run:
    loop.run = replace(loop.run, status=status, ended_at=clock.now())
    runs.save(conn, loop.run)
    locks.release(conn, loop.id)
    return loop.run


@dataclass
class Pacer:
    """wait_until 由组装根提供：固定时钟(--now)直接拨快，系统时钟睡眠。"""

    clock: Clock
    wait_until: Callable[[datetime], None]
    last: dict[str, datetime] = field(default_factory=dict)

    def mark(self, key: str, at: datetime) -> None:
        self.last[key] = at.replace(microsecond=0)

    def call(self, key: str, action: Callable[[], Any]) -> Any:
        second = self.clock.now().replace(microsecond=0)
        previous = self.last.get(key)
        if previous is not None and second <= previous:
            self.wait_until(previous + timedelta(seconds=1))
            second = self.clock.now().replace(microsecond=0)
        self.last[key] = second
        return action()
