"""恢复与控制(protocol/recovery.md)：检查点、中断时就地收尾、启动时恢复、运行控制(暂停、急停、恢复、接管、交还)。

- 检查点就是每一步的 `handoff.json`，写好才算这一步完成；没做完的那一步整个丢掉，worktree 退回上一个检查点的 commit；
- 中断收尾与启动恢复只改 runs 表的状态、释放锁、记事件；被中断的对象停在中断前的状态，下次从检查点接着做；
- 写操作的幂等键在 store/tables/operations.py(run_once、complete、abandon)，状态不明的由 protocol/git 先到远端对账。
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from tightrein.protocol import handoff
from tightrein.protocol.handoff import Handoff
from tightrein.protocol.naming import Clock, format_iso, kind_of
from tightrein.protocol.process import Interrupted
from tightrein.settings.load import Settings
from tightrein.store.files.json import read_json, write_json
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.tables import issues, runs

if TYPE_CHECKING:
    from tightrein.protocol.records import EventLog

POINT = "protocol.recovery"
# 实施各步完成时，程序把 worktree 的 commit 记在必填事实的这个字段里，作为退回的落点
CHECKPOINT_COMMIT = "worktreeCommit"
INTERRUPTED = "interrupted"
FAILED = "failed"

_HANDOFF_FILE = re.compile(r"^(\d{2})-(.+?)(?:\.r(\d+))?-handoff\.json$")


class Mode(StrEnum):
    PAUSED = "paused"  # 当前这一步做完后停下，不再开始新的
    STOPPED = "stopped"  # 急停：立刻停掉所有工作


@dataclass(frozen=True)
class Control:
    mode: Mode
    since: str
    note: str | None = None


@dataclass(frozen=True)
class Checkpoint:
    path: Path
    sequence: int
    round: int | None
    handoff: Handoff

    @property
    def commit(self) -> str | None:
        value = self.handoff.facts.get(CHECKPOINT_COMMIT)
        return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class Closed:
    run: str
    status: str
    reason: str


class Releasable(Protocol):
    def release(self) -> None: ...


# 检查点


def checkpoints(layout: WorkspaceLayout, subject: str) -> list[Checkpoint]:
    """对象目录中已完成的步骤，按完成的先后排列。"""
    directory = layout.subject_dir(subject)
    if not directory.is_dir():
        return []
    found = []
    for path in directory.iterdir():
        match = _HANDOFF_FILE.match(path.name)
        if match is None:
            continue
        found.append(
            Checkpoint(
                path=path,
                sequence=int(match.group(1)),
                round=int(match.group(3)) if match.group(3) else None,
                handoff=handoff.read(path),
            )
        )
    # 不能只按文件名排：编码第 2 轮(35…r2)在审查第 1 轮(37…r1)之后才完成。先按写入时间(只到秒)，
    # 同一秒内完成的按文件的修改时间(纳秒)：不带轮次的步骤(交付)不会排到同一秒的审查前面
    return sorted(found, key=lambda item: (item.handoff.created_at or "", item.path.stat().st_mtime_ns,
                                           item.sequence))


def last_checkpoint(layout: WorkspaceLayout, subject: str) -> Checkpoint | None:
    found = checkpoints(layout, subject)
    return found[-1] if found else None


def checkpoint_commit(layout: WorkspaceLayout, subject: str) -> str | None:
    """最近一个记了 worktree commit 的检查点的 commit。"""
    for item in reversed(checkpoints(layout, subject)):
        if item.commit is not None:
            return item.commit
    return None


def rewind(
    worktree: Path,
    layout: WorkspaceLayout,
    subject: str,
    *,
    base: str,
    reset: Callable[[Path, str], None],
) -> str:
    """worktree 退回上一个检查点的 commit(还没有时退回开始时的 base)，不续接半截状态；返回退回到的 commit。

    reset 由调用方注入(protocol/git 的 `reset --hard` 加清理未跟踪文件)。
    """
    commit = checkpoint_commit(layout, subject) or base
    reset(worktree, commit)
    return commit


# 中断时就地收尾


def closing_status(failure: BaseException | None) -> tuple[str, str]:
    """命令结束时仍在进行中的运行该标成什么：被中断(Ctrl-C、SIGTERM、SIGHUP)为中断，其余为失败。"""
    if isinstance(failure, (KeyboardInterrupt, Interrupted)):
        return INTERRUPTED, "命令被中断"
    if failure is not None:
        return FAILED, f"命令出错：{type(failure).__name__}"
    return FAILED, "命令结束时运行仍为进行中"


def close_own(
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    pid: int,
    host: str,
    failure: BaseException | None,
    locks: Iterable[Releasable],
    events_for: Callable[[str], EventLog],
) -> list[Closed]:
    """本进程开始、仍为进行中的运行标为中断或失败，并释放本进程持有的锁。

    不依赖锁来认运行：锁多由 with 块持有，中断经过时已先释放。收尾出错时由调用方记下、不覆盖命令原来的结果，
    下次启动恢复兜底。
    """
    status, reason = closing_status(failure)
    for lock in locks:
        lock.release()
    closed = []
    for run in runs.running(conn):
        if (run.holder_pid, run.holder_host) != (pid, host):
            continue
        runs.finish(conn, run.id, status, clock)
        _record(events_for, run.id, f"{reason}，运行标为 {status}")
        closed.append(Closed(run.id, status, reason))
    return closed


def shutdown_grace(settings: Settings) -> float:
    return settings.duration("limits.shutdownGrace")


def within_grace(
    grace_s: float,
    action: Callable[[], None],
    *,
    code: int,
    force_exit: Callable[[int], None] = os._exit,
) -> None:
    """在宽限期内做完收尾；超时直接以 code 结束进程，不让卡住的收尾拖住退出。"""
    timer = threading.Timer(grace_s, force_exit, args=(code,))
    timer.daemon = True
    timer.start()
    try:
        action()
    finally:
        timer.cancel()


# 启动时恢复


def recover(
    conn: sqlite3.Connection,
    clock: Clock,
    settings: Settings,
    *,
    host: str,
    alive: Callable[[int], bool],
    events_for: Callable[[str], EventLog],
    current: str | None = None,
) -> list[Closed]:
    """每次运行开始时：心跳失效，或同主机且开始它的进程已不在的进行中运行，标为中断。

    进程号在运行插入时记下、之后不改，判断不依赖锁；其他主机的只看心跳。
    """
    stale_s = settings.duration("limits.lock.stale")
    now = clock.now()
    closed = []
    for run in runs.running(conn):
        if run.id == current:
            continue
        dead = run.holder_host == host and run.holder_pid is not None and not alive(run.holder_pid)
        last = run.heartbeat_at or run.started_at
        if not dead and (now - last).total_seconds() <= stale_s:
            continue
        reason = "开始它的进程已不在" if dead else f"超过 {settings.get('limits.lock.stale')} 没有心跳"
        runs.finish(conn, run.id, INTERRUPTED, clock)
        _record(events_for, run.id, f"{reason}，运行标为 {INTERRUPTED}，对象下次从检查点接着做")
        closed.append(Closed(run.id, INTERRUPTED, reason))
    return closed


# 运行控制


def control(layout: WorkspaceLayout) -> Control | None:
    """当前的运行控制状态；没有暂停或急停时为空。调度在开始每一步与每个新对象前查它。"""
    if not layout.control_file.is_file():
        return None
    data = read_json(layout.control_file)
    return Control(mode=Mode(data["mode"]), since=data["since"], note=data.get("note"))


def pause(layout: WorkspaceLayout, clock: Clock, note: str | None = None) -> None:
    """当前这一步做完后停下；用户当场发起的命令不受影响。"""
    _write_control(layout, Mode.PAUSED, clock, note)


def stop(
    layout: WorkspaceLayout,
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    host: str,
    terminate: Callable[[int], None],
    note: str | None = None,
) -> list[str]:
    """急停：记下急停，并让本机所有进行中的运行的进程收到终止信号；返回被终止的运行。

    terminate 由调用方注入(发 SIGTERM)；那边的入口把信号转成 Interrupted，经 close_own 标为中断。
    没做完的那一步不留半截：下次恢复后由 rewind 退回上一个检查点。
    """
    _write_control(layout, Mode.STOPPED, clock, note)
    stopped = []
    for run in runs.running(conn):
        if run.holder_host == host and run.holder_pid is not None:
            terminate(run.holder_pid)
            stopped.append(run.id)
    return stopped


def resume(layout: WorkspaceLayout) -> Control | None:
    """从暂停或急停恢复；返回之前的状态(没有暂停时为空)。"""
    previous = control(layout)
    layout.control_file.unlink(missing_ok=True)
    return previous


def take(conn: sqlite3.Connection, clock: Clock, subject: str, *, by: str) -> issues.Issue:
    """手动接管：记下接管者，tightrein 不再碰这个 Issue。"""
    return _hold(conn, clock, subject, by)


def give(conn: sqlite3.Connection, clock: Clock, subject: str) -> issues.Issue:
    """交还给 tightrein，从检查点接着做。"""
    return _hold(conn, clock, subject, None)


def _hold(conn: sqlite3.Connection, clock: Clock, subject: str, by: str | None) -> issues.Issue:
    if kind_of(subject) != "issue":
        raise ValueError(f"只能接管或交还 Issue：{subject}")
    issue = issues.get(conn, subject)
    if issue is None:
        raise LookupError(f"没有 Issue {subject}")
    updated = replace(issue, held_by=by)
    issues.save(conn, updated, clock)
    return updated


def _write_control(layout: WorkspaceLayout, mode: Mode, clock: Clock, note: str | None) -> None:
    write_json(layout.control_file, {"mode": mode.value, "since": format_iso(clock.now()), "note": note})


def _record(events_for: Callable[[str], EventLog], run: str, summary: str) -> None:
    events_for(run).emit(run=run, subject=None, point=POINT, kind="decision", summary=summary)


__all__ = [
    "CHECKPOINT_COMMIT",
    "Checkpoint",
    "Closed",
    "Control",
    "Mode",
    "checkpoint_commit",
    "checkpoints",
    "close_own",
    "closing_status",
    "control",
    "give",
    "last_checkpoint",
    "pause",
    "recover",
    "resume",
    "rewind",
    "shutdown_grace",
    "stop",
    "take",
    "within_grace",
]
