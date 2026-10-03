"""对象锁、具名锁与文件锁(architecture/01 4.4)。

对象锁存放在 locks 表中，一个对象一行，记录持有者的进程号、主机名、所属运行、开始时间与超时时间。
超时的锁、或同一主机上持有者进程已不存在的锁视为失效，可以被接管；接管时返回被接管的锁，由调用方记录事件。
其他主机上的持有者无法检查进程，只按超时判断。具名锁是固定编号的对象锁。
文件锁用 flock，进程退出时由操作系统释放，不会残留。
"""

from __future__ import annotations

import fcntl
import os
import socket
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from tightrein.config import layers
from tightrein.domain.clock import Clock, format_iso
from tightrein.store.db import transaction
from tightrein.store.repos.table import TIME, Table

KNOWLEDGE = "knowledge"
LOCAL_RUN = "local-run"


@dataclass(frozen=True)
class Holder:
    pid: int
    host: str


def current_holder() -> Holder:
    return Holder(os.getpid(), socket.gethostname())


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class ObjectLock:
    subject_id: str
    holder_pid: int
    holder_host: str
    acquired_at: datetime
    expires_at: datetime
    run_id: str | None = None

    @property
    def holder(self) -> Holder:
        return Holder(self.holder_pid, self.holder_host)


@dataclass(frozen=True)
class Acquired:
    lock: ObjectLock
    taken_over: ObjectLock | None = None


class LockHeld(Exception):
    """对象锁由其他持有者持有且仍然有效。"""

    def __init__(self, lock: ObjectLock) -> None:
        self.lock = lock
        super().__init__(
            f"{lock.subject_id} 由主机 {lock.holder_host} 的进程 {lock.holder_pid} 持有"
            f"(运行 {lock.run_id or '无'})，{format_iso(lock.expires_at)} 超时"
        )


class LockNotHeld(Exception):
    """要延长的锁不存在或已被其他持有者接管。"""


class FileLockBusy(Exception):
    """不等待的文件锁已被其他进程持有。"""


TABLE = Table("locks", ObjectLock, ("subject_id",), {"acquired_at": TIME, "expires_at": TIME})


def get(conn: sqlite3.Connection, subject_id: str) -> ObjectLock | None:
    return TABLE.get(conn, subject_id=subject_id)


def is_stale(lock: ObjectLock, now: datetime, host: str, alive: Callable[[int], bool] = process_alive) -> bool:
    return now >= lock.expires_at or (lock.holder_host == host and not alive(lock.holder_pid))


def _try_acquire(
    conn: sqlite3.Connection,
    subject_id: str,
    now: datetime,
    ttl: timedelta,
    run_id: str | None,
    holder: Holder,
    alive: Callable[[int], bool],
) -> Acquired:
    lock = ObjectLock(subject_id, holder.pid, holder.host, now, now + ttl, run_id)
    with transaction(conn):
        current = get(conn, subject_id)
        if current is not None and current.holder != holder and not is_stale(current, now, holder.host, alive):
            raise LockHeld(current)
        TABLE.save(conn, lock)
    taken_over = current if current is not None and current.holder != holder else None
    return Acquired(lock, taken_over)


def acquire(
    conn: sqlite3.Connection,
    subject_id: str,
    clock: Clock,
    ttl: timedelta,
    *,
    run_id: str | None = None,
    wait: timedelta = timedelta(0),
    poll: timedelta | None = None,
    sleep: Callable[[float], None] = time.sleep,
    holder: Holder | None = None,
    alive: Callable[[int], bool] = process_alive,
) -> Acquired:
    """获取对象锁，有效期为 ttl；同一持有者再次获取时刷新有效期。

    锁被他人有效持有时，每隔 poll(缺省取 runtime.store.lockPollSeconds 的核心缺省值)重试一次，直到等待了 wait
    仍未获取则抛出 LockHeld；wait 为 0 时立即抛出。
    """
    interval = poll if poll is not None else timedelta(
        seconds=float(layers.core_value("runtime.store.lockPollSeconds")))
    owner = holder or current_holder()
    deadline = clock.now() + wait
    while True:
        try:
            return _try_acquire(conn, subject_id, clock.now(), ttl, run_id, owner, alive)
        except LockHeld:
            if clock.now() >= deadline:
                raise
            sleep(interval.total_seconds())


def refresh(
    conn: sqlite3.Connection, subject_id: str, clock: Clock, ttl: timedelta, holder: Holder | None = None
) -> ObjectLock:
    """延长自己持有的锁，有效期从当前时间起算。"""
    owner = holder or current_holder()
    with transaction(conn):
        current = get(conn, subject_id)
        if current is None or current.holder != owner:
            raise LockNotHeld(f"{subject_id} 的锁不存在或已被其他持有者接管")
        updated = replace(current, expires_at=clock.now() + ttl)
        TABLE.save(conn, updated)
    return updated


def release(conn: sqlite3.Connection, subject_id: str, holder: Holder | None = None) -> bool:
    """释放自己持有的锁；锁不存在或已被他人接管时不做任何事，返回 False。"""
    owner = holder or current_holder()
    cursor = conn.execute(
        "DELETE FROM locks WHERE subject_id = ? AND holder_pid = ? AND holder_host = ?",
        (subject_id, owner.pid, owner.host),
    )
    return cursor.rowcount > 0


@contextmanager
def held(
    conn: sqlite3.Connection,
    subject_id: str,
    clock: Clock,
    ttl: timedelta,
    *,
    run_id: str | None = None,
    wait: timedelta = timedelta(0),
    poll: timedelta | None = None,
    sleep: Callable[[float], None] = time.sleep,
    holder: Holder | None = None,
    alive: Callable[[int], bool] = process_alive,
) -> Iterator[Acquired]:
    """在代码块期间持有对象锁，结束或出错时释放。"""
    acquired = acquire(conn, subject_id, clock, ttl, run_id=run_id, wait=wait, poll=poll, sleep=sleep,
                       holder=holder, alive=alive)
    try:
        yield acquired
    finally:
        release(conn, subject_id, acquired.lock.holder)


@contextmanager
def file_lock(path: Path, *, wait: bool = True) -> Iterator[None]:
    """在代码块期间持有文件锁。wait 为真时排队等待(聚合)，为假时已被持有即抛出 FileLockBusy(编排运行)。

    锁文件中写入持有者的进程号，只供排查。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        flags = fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as error:
            raise FileLockBusy(f"{path} 正被其他进程持有") from error
        try:
            handle.truncate(0)
            handle.write(f"{os.getpid()}\n")
            handle.flush()
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
