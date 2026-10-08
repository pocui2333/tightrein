import sqlite3
from datetime import timedelta

import pytest

from tightrein.protocol.naming import FixedClock
from tightrein.store.tables import runs
from tightrein.store.tables.runs import Run


def new_run(conn: sqlite3.Connection, clock: FixedClock, stage: str = "collect") -> Run:
    run = Run(runs.free_id(conn, clock.now(), stage), stage, "schedule", "running", clock.now())
    runs.start(conn, run)
    return run


def test_start_records_the_holder_and_first_heartbeat(conn: sqlite3.Connection, clock: FixedClock) -> None:
    run = new_run(conn, clock)
    stored = runs.get(conn, run.id)
    assert stored is not None
    assert stored.id == "R-20261007T093000Z-collect"
    assert stored.holder_pid is not None and stored.holder_host
    assert stored.heartbeat_at == clock.now()


def test_free_id_moves_to_the_next_second_when_taken(conn: sqlite3.Connection, clock: FixedClock) -> None:
    first = new_run(conn, clock)
    second = new_run(conn, clock)
    assert (first.id, second.id) == ("R-20261007T093000Z-collect", "R-20261007T093001Z-collect")
    assert runs.free_id(conn, clock.now(), "assess") == "R-20261007T093000Z-assess"


def test_heartbeat_and_finish(conn: sqlite3.Connection, clock: FixedClock) -> None:
    run = new_run(conn, clock)
    clock.advance(timedelta(seconds=30))
    runs.heartbeat(conn, run.id, clock)
    assert runs.get(conn, run.id).heartbeat_at == clock.now()  # type: ignore[union-attr]
    assert [item.id for item in runs.running(conn)] == [run.id]
    clock.advance(timedelta(minutes=5))
    runs.finish(conn, run.id, "done", clock, {"problems": 3})
    finished = runs.get(conn, run.id)
    assert finished is not None
    assert (finished.status, finished.ended_at, finished.summary) == ("done", clock.now(), {"problems": 3})
    assert runs.running(conn) == []
    with pytest.raises(ValueError):
        runs.finish(conn, run.id, "running", clock)


def test_latest_by_stage(conn: sqlite3.Connection, clock: FixedClock) -> None:
    collect = new_run(conn, clock)
    clock.advance(timedelta(hours=1))
    implement = new_run(conn, clock, "implement")
    assert runs.latest(conn).id == implement.id  # type: ignore[union-attr]
    assert runs.latest(conn, "collect").id == collect.id  # type: ignore[union-attr]
    assert runs.latest(conn, "retro") is None


def test_values_are_checked(clock: FixedClock) -> None:
    with pytest.raises(ValueError):
        Run("R-20261007T093000Z-collect", "collect", "cron", "running", clock.now())
    with pytest.raises(ValueError):
        Run("R-20261007T093000Z-collect", "collect", "manual", "ok", clock.now())
