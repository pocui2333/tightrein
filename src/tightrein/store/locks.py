"""文件锁加心跳(limits.md「锁(心跳)」)：运行锁 data/run.lock、对象锁 data/locks/<编号>.lock。

锁文件里是持有者(进程号、主机、取得时间、心跳时间)的 JSON。谁持有以文件内容为准；flock 只包住
「读持有者 → 判断 → 写持有者」这一小段，使多个进程的判断与写入不交错。
不让 flock 覆盖整个持有期，是因为卡死的进程仍握着 flock，别人永远接管不了；按心跳判断失效才能在 90 秒内接管。

失效：心跳超过 stale_s；或持有者在本机且进程已不存在(本机可以立即判断，不必等心跳超时)。
其他主机上的持有者无法检查进程，只按心跳判断。
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from tightrein.protocol.naming import Clock, format_iso, parse_iso


class Busy(Exception):
    """锁被他人持有且未失效。"""

    def __init__(self, path: Path, holder: dict[str, Any]) -> None:
        super().__init__(
            f"{path.name} 由主机 {holder.get('host')} 的进程 {holder.get('pid')} 持有，"
            f"最近心跳 {holder.get('heartbeatAt')}"
        )
        self.holder = holder


class Lost(Exception):
    """要续心跳的锁已不是自己的(已失效并被他人接管)。"""

    def __init__(self, path: Path, holder: dict[str, Any] | None) -> None:
        super().__init__(f"{path.name} 已不由本进程持有")
        self.holder = holder


class FileLock:
    def __init__(
        self,
        path: Path,
        clock: Clock,
        *,
        stale_s: float,
        poll_s: float = 1.0,
    ) -> None:
        """poll_s：wait=True 时多久再试一次。"""
        self.path = path
        self._clock = clock
        self._stale_s = stale_s
        self._poll_s = poll_s
        self._pid = os.getpid()
        self._host = socket.gethostname()

    def acquire(self, *, wait: bool = False) -> dict[str, Any] | None:
        """取得锁，返回被接管的旧持有者(由调用方记事件)，原本空闲或本来就是自己的返回 None。

        被占且未失效时：wait 为假即抛 Busy；为真则每隔 poll_s 再试，直到取得。
        """
        while True:
            try:
                return self._try_acquire()
            except Busy:
                if not wait:
                    raise
                time.sleep(self._poll_s)

    def beat(self) -> None:
        """写心跳时间；锁已被他人接管时抛 Lost，调用方应停下手上的工作。"""
        with self._exclusive() as descriptor:
            current = _read(descriptor)
            if current is None or not self._ours(current):
                raise Lost(self.path, current)
            current["heartbeatAt"] = format_iso(self._clock.now())
            _write(descriptor, current)

    def release(self) -> None:
        """只释放自己的锁；已被他人接管时不动。"""
        with self._exclusive() as descriptor:
            current = _read(descriptor)
            if current is not None and self._ours(current):
                _write(descriptor, None)

    def holder(self) -> dict[str, Any] | None:
        """当前持有者(供 status 与排查)；空闲时为 None。"""
        with self._exclusive() as descriptor:
            return _read(descriptor)

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.release()

    def _try_acquire(self) -> dict[str, Any] | None:
        with self._exclusive() as descriptor:
            current = _read(descriptor)
            if current is not None and not self._ours(current) and not self._stale(current):
                raise Busy(self.path, current)
            now = format_iso(self._clock.now())
            _write(descriptor, {"pid": self._pid, "host": self._host, "acquiredAt": now, "heartbeatAt": now})
            return None if current is None or self._ours(current) else current

    def _ours(self, holder: dict[str, Any]) -> bool:
        return holder.get("pid") == self._pid and holder.get("host") == self._host

    def _stale(self, holder: dict[str, Any]) -> bool:
        try:
            heartbeat = parse_iso(holder["heartbeatAt"])
            pid = int(holder["pid"])
        except (KeyError, TypeError, ValueError):
            return True  # 内容残缺(写到一半时进程被杀)，没有可信的持有者
        if (self._clock.now() - heartbeat).total_seconds() > self._stale_s:
            return True
        return holder.get("host") == self._host and not process_alive(pid)

    @contextmanager
    def _exclusive(self) -> Iterator[int]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield descriptor
        finally:
            os.close(descriptor)  # 关闭即释放 flock


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在，只是属于别的用户
    return True


def _read(descriptor: int) -> dict[str, Any] | None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 4096):
        chunks.append(chunk)
    text = b"".join(chunks).decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}  # 残缺内容：交给 _stale 判为失效
    return value if isinstance(value, dict) else {}


def _write(descriptor: int, holder: dict[str, Any] | None) -> None:
    # 原地改写而不是临时文件加改名：改名会换掉文件，别的进程手里的 flock 就落在旧文件上，互斥失效
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    if holder is not None:
        os.write(descriptor, (json.dumps(holder, ensure_ascii=False) + "\n").encode("utf-8"))
