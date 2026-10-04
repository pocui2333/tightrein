"""中断识别与接管(architecture/09 3.5)。在每次 run 与 continue 开始时执行；命令结束时另由 close_own 就地收尾。

接管 ended_at 为空、状态为 running 的两类运行，改为 interrupted、写入 ended_at，释放它持有的对象锁并写 gate 事件：
1. 开始它的进程(runs.holder_pid、holder_host)在本机并已不存在的运行；迁移 014 之前开始、没有记录进程的运行，
   看它持有的对象锁：持有对象锁、且这些锁的持有进程都在本机并已不存在(loop 运行在运行期间总持有以自身编号为对象的锁)；
2. 上一类中 loop 运行的子运行。
其余运行(进程仍在、在其他主机上，或没有进程记录也没有锁)可能是另一个终端中正在执行的命令，不处理。被中断的对象
停在中断前的状态，由下一次 continue 或 run 的对应步骤按状态表重新执行(各模块只在一步完成时更新状态，对外操作由
幂等键保证不重复)。

close_own：命令结束时(cli/main)把本进程开始、仍为 running 的运行改为 interrupted(被 Ctrl+C、SIGTERM、SIGHUP
中断)或 failed(异常，或模块漏了结束运行)，并释放本进程持有的全部对象锁。对象锁多由上下文管理器持有，中断经过时
已先释放，没有进程记录的运行就再也认不出，所以收尾不依赖锁。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace

from tightrein.domain.clock import Clock
from tightrein.domain.enums import RunStage, RunStatus
from tightrein.observability.events import EventLog
from tightrein.observability.tracing import Tracer
from tightrein.store import locks
from tightrein.store.repos import runs

TAKEOVER = "takeover"


@dataclass(frozen=True)
class Recovered:
    run_id: str
    released: tuple[str, ...]


def interrupted(conn: sqlite3.Connection, *, alive: Callable[[int], bool], host: str,
                current: str | None = None) -> set[str]:
    """状态为 running 但已中断的运行(上面两类)；只读，tightrein watch 据此不把它们显示为进行中。"""
    running = [run for run in runs.find(conn, status=RunStatus.RUNNING) if run.id != current]
    dead = set()
    for run in running:
        if run.holder_pid is not None:
            if run.holder_host == host and not alive(run.holder_pid):
                dead.add(run.id)
            continue
        held = locks.TABLE.find(conn, run_id=run.id)
        if held and all(lock.holder_host == host and not alive(lock.holder_pid) for lock in held):
            dead.add(run.id)
    loops = {run.id for run in running if run.stage is RunStage.LOOP and run.id in dead}
    return dead | {run.id for run in running if run.parent_run_id in loops}


def recover(conn: sqlite3.Connection, clock: Clock, events: EventLog, *, alive: Callable[[int], bool], host: str,
            current: str | None = None) -> list[Recovered]:
    gone = interrupted(conn, alive=alive, host=host, current=current)
    recovered = []
    for run in runs.find(conn, status=RunStatus.RUNNING):
        if run.id not in gone:
            continue
        found = locks.TABLE.find(conn, run_id=run.id)
        for lock in found:
            locks.release(conn, lock.subject_id, lock.holder)
        runs.save(conn, replace(run, status=RunStatus.INTERRUPTED, ended_at=clock.now()))
        released = tuple(lock.subject_id for lock in found)
        Tracer(events, clock, run_id=run.id, stage=run.stage.value).event(
            "gate", decision=TAKEOVER, reason="运行已中断，接管并释放对象锁", attributes={"released": list(released)})
        recovered.append(Recovered(run.id, released))
    return recovered


def close_own(conn: sqlite3.Connection, clock: Clock, events: EventLog, *, holder: locks.Holder, status: RunStatus,
              reason: str) -> list[Recovered]:
    """收尾 holder 开始、仍为 running 的运行(改为 status、写入 ended_at 与 gate 事件)，并释放 holder 持有的全部对象锁。"""
    held = locks.TABLE.find(conn, holder_pid=holder.pid, holder_host=holder.host)
    for lock in held:
        locks.release(conn, lock.subject_id, holder)
    closed = []
    for run in runs.find(conn, status=RunStatus.RUNNING, holder_pid=holder.pid, holder_host=holder.host):
        runs.save(conn, replace(run, status=status, ended_at=clock.now()))
        released = tuple(lock.subject_id for lock in held if lock.run_id == run.id)
        Tracer(events, clock, run_id=run.id, stage=run.stage.value).event(
            "gate", decision=status.value, reason=reason, attributes={"released": list(released)})
        closed.append(Recovered(run.id, released))
    return closed
