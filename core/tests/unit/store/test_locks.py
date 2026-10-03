import os
import subprocess
import sys
import threading
from datetime import timedelta

import pytest

from tightrein.store import locks
from tightrein.store.locks import FileLockBusy, Holder, LockHeld, LockNotHeld

TTL = timedelta(minutes=30)
ME = Holder(100, "mac-a")
OTHER = Holder(200, "mac-a")
REMOTE = Holder(300, "mac-b")


def alive(pid):
    return pid != 999


def acquire(conn, clock, holder, **kwargs):
    return locks.acquire(conn, "0007", clock, TTL, holder=holder, alive=alive, **kwargs)


def test_acquire_a_free_lock(conn, clock):
    acquired = acquire(conn, clock, ME, run_id="R-20260929-021503-fix")
    assert acquired.taken_over is None
    assert locks.get(conn, "0007") == acquired.lock
    assert (acquired.lock.acquired_at, acquired.lock.expires_at) == (clock.now(), clock.now() + TTL)


def test_a_valid_lock_held_by_another_process_is_refused(conn, clock):
    first = acquire(conn, clock, OTHER, run_id="R-20260929-021503-fix")
    with pytest.raises(LockHeld, match="进程 200") as error:
        acquire(conn, clock, ME)
    assert error.value.lock == first.lock
    assert locks.get(conn, "0007") == first.lock


def test_the_same_holder_refreshes_its_lock(conn, clock):
    acquire(conn, clock, ME)
    clock.advance(timedelta(minutes=10))
    again = acquire(conn, clock, ME)
    assert again.taken_over is None
    assert locks.get(conn, "0007").expires_at == clock.now() + TTL


def test_an_expired_lock_is_taken_over(conn, clock):
    old = acquire(conn, clock, REMOTE).lock
    clock.advance(TTL)
    acquired = acquire(conn, clock, ME)
    assert acquired.taken_over == old
    assert locks.get(conn, "0007").holder == ME


def test_a_lock_of_a_dead_local_process_is_taken_over(conn, clock):
    dead = acquire(conn, clock, Holder(999, "mac-a")).lock
    assert acquire(conn, clock, ME).taken_over == dead


def test_a_remote_holder_is_only_judged_by_expiry(conn, clock):
    acquire(conn, clock, Holder(999, "mac-b"))
    with pytest.raises(LockHeld):
        acquire(conn, clock, ME)


def test_waiting_until_the_lock_expires(conn, clock):
    acquire(conn, clock, OTHER)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(timedelta(seconds=seconds))

    acquired = acquire(conn, clock, ME, wait=TTL + timedelta(minutes=1), poll=timedelta(minutes=10), sleep=sleep)
    assert acquired.lock.holder == ME
    assert sleeps == [600.0, 600.0, 600.0]


def test_waiting_gives_up_after_the_wait(conn, clock):
    acquire(conn, clock, OTHER)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(timedelta(seconds=seconds))

    with pytest.raises(LockHeld):
        acquire(conn, clock, ME, wait=timedelta(minutes=15), poll=timedelta(minutes=10), sleep=sleep)
    assert sleeps == [600.0, 600.0]


def test_release_only_removes_our_own_lock(conn, clock):
    acquire(conn, clock, OTHER)
    assert locks.release(conn, "0007", ME) is False
    assert locks.get(conn, "0007") is not None
    assert locks.release(conn, "0007", OTHER) is True
    assert locks.get(conn, "0007") is None
    assert locks.release(conn, "0007", OTHER) is False


def test_refresh_extends_only_our_own_lock(conn, clock):
    acquire(conn, clock, ME)
    clock.advance(timedelta(minutes=20))
    assert locks.refresh(conn, "0007", clock, TTL, ME).expires_at == clock.now() + TTL
    with pytest.raises(LockNotHeld):
        locks.refresh(conn, "0007", clock, TTL, OTHER)
    with pytest.raises(LockNotHeld):
        locks.refresh(conn, "0008", clock, TTL, ME)


def test_held_releases_even_on_error(conn, clock):
    with pytest.raises(RuntimeError):
        with locks.held(conn, locks.KNOWLEDGE, clock, TTL, holder=ME, alive=alive) as acquired:
            assert locks.get(conn, locks.KNOWLEDGE) == acquired.lock
            raise RuntimeError("中途失败")
    assert locks.get(conn, locks.KNOWLEDGE) is None


def test_process_alive():
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    assert locks.process_alive(finished.pid) is False
    assert locks.current_holder().pid == os.getpid()
    assert locks.process_alive(locks.current_holder().pid) is True


def test_file_lock_without_waiting(tmp_path):
    path = tmp_path / "data" / "run.lock"
    with locks.file_lock(path, wait=False):
        assert path.read_text(encoding="utf-8").strip().isdigit()
        with pytest.raises(FileLockBusy, match="run.lock"):
            with locks.file_lock(path, wait=False):
                pass
    with locks.file_lock(path, wait=False):
        pass


def test_file_lock_waits_in_line(tmp_path):
    path = tmp_path / "aggregate.lock"
    order = []
    holding = threading.Event()

    def second():
        holding.wait()
        with locks.file_lock(path):
            order.append("second")

    thread = threading.Thread(target=second)
    thread.start()
    with locks.file_lock(path):
        holding.set()
        thread.join(timeout=0.2)
        order.append("first")
    thread.join()
    assert order == ["first", "second"]
