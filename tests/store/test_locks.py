import json
import os
import socket
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tightrein.protocol.naming import FixedClock, format_iso
from tightrein.store.locks import Busy, FileLock, Lost, process_alive

STALE_S = 90.0


def lock(path: Path, clock: FixedClock, **kwargs: Any) -> FileLock:
    return FileLock(path, clock, stale_s=STALE_S, **kwargs)


def dead_pid() -> int:
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    return finished.pid


def write_holder(path: Path, clock: FixedClock, *, pid: int, host: str) -> dict[str, Any]:
    holder = {"pid": pid, "host": host, "acquiredAt": format_iso(clock.now()), "heartbeatAt": format_iso(clock.now())}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(holder), encoding="utf-8")
    return holder


def test_acquire_a_free_lock_writes_the_holder(tmp_path: Path, clock: FixedClock) -> None:
    path = tmp_path / "data" / "run.lock"
    run_lock = lock(path, clock)
    assert run_lock.acquire() is None
    holder = json.loads(path.read_text(encoding="utf-8"))
    assert holder == {"pid": os.getpid(), "host": socket.gethostname(),
                      "acquiredAt": "2026-10-07T09:30:00Z", "heartbeatAt": "2026-10-07T09:30:00Z"}
    assert run_lock.acquire() is None  # 本来就是自己的


def test_a_live_holder_is_refused(tmp_path: Path, clock: FixedClock) -> None:
    path = tmp_path / "run.lock"
    holder = write_holder(path, clock, pid=os.getppid(), host=socket.gethostname())
    with pytest.raises(Busy, match=f"进程 {os.getppid()}") as error:
        lock(path, clock).acquire()
    assert error.value.holder == holder


def test_a_lock_of_a_dead_local_process_is_taken_over(tmp_path: Path, clock: FixedClock) -> None:
    path = tmp_path / "run.lock"
    old = write_holder(path, clock, pid=dead_pid(), host=socket.gethostname())
    assert lock(path, clock).acquire() == old
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_a_remote_holder_is_only_judged_by_heartbeat(tmp_path: Path, clock: FixedClock) -> None:
    path = tmp_path / "run.lock"
    old = write_holder(path, clock, pid=dead_pid(), host="another-mac")
    with pytest.raises(Busy):
        lock(path, clock).acquire()
    clock.advance(timedelta(seconds=STALE_S + 1))
    assert lock(path, clock).acquire() == old


def test_beating_keeps_the_lock_alive(tmp_path: Path, clock: FixedClock) -> None:
    path = tmp_path / "run.lock"
    write_holder(path, clock, pid=os.getppid(), host="another-mac")
    clock.advance(timedelta(seconds=STALE_S + 1))
    mine = lock(path, clock)
    mine.acquire()
    clock.advance(timedelta(seconds=60))
    mine.beat()
    assert mine.holder()["heartbeatAt"] == format_iso(clock.now())  # type: ignore[index]


def test_beat_after_a_takeover_raises_lost(tmp_path: Path, clock: FixedClock) -> None:
    path = tmp_path / "run.lock"
    mine = lock(path, clock)
    mine.acquire()
    other = write_holder(path, clock, pid=os.getppid(), host="another-mac")
    with pytest.raises(Lost) as error:
        mine.beat()
    assert error.value.holder == other


def test_release_only_removes_our_own_lock(tmp_path: Path, clock: FixedClock) -> None:
    path = tmp_path / "run.lock"
    other = write_holder(path, clock, pid=os.getppid(), host="another-mac")
    mine = lock(path, clock)
    mine.release()
    assert mine.holder() == other
    clock.advance(timedelta(seconds=STALE_S + 1))
    mine.acquire()
    mine.release()
    assert mine.holder() is None
    assert lock(path, clock).acquire() is None


def test_a_half_written_holder_is_taken_over(tmp_path: Path, clock: FixedClock) -> None:
    path = tmp_path / "run.lock"
    path.write_text('{"pid": 12', encoding="utf-8")
    assert lock(path, clock).acquire() == {}


def test_with_releases_even_on_error(tmp_path: Path, clock: FixedClock) -> None:
    path = tmp_path / "locks" / "0018.lock"
    with pytest.raises(RuntimeError), lock(path, clock) as held:
        assert held.holder() is not None
        raise RuntimeError("中途失败")
    assert lock(path, clock).holder() is None


def test_waiting_takes_the_lock_once_it_is_released(tmp_path: Path, clock: FixedClock) -> None:
    path = tmp_path / "run.lock"
    write_holder(path, clock, pid=os.getppid(), host=socket.gethostname())
    released = threading.Timer(0.1, lambda: path.write_text("", encoding="utf-8"))
    released.start()
    assert lock(path, clock, poll_s=0.02).acquire(wait=True) is None
    released.join()
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_process_alive() -> None:
    assert process_alive(dead_pid()) is False
    assert process_alive(os.getpid()) is True
