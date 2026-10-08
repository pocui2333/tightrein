import sqlite3
from datetime import timedelta

from tightrein.protocol.naming import FixedClock
from tightrein.store.tables import counters


def test_add_accumulates_and_reset_restarts_the_window(conn: sqlite3.Connection, clock: FixedClock) -> None:
    assert counters.get(conn, "tokens:0018") == 0.0
    assert counters.entry(conn, "tokens:0018") is None
    assert counters.add(conn, "tokens:0018", 1200, clock) == 1200
    clock.advance(timedelta(minutes=5))
    assert counters.add(conn, "tokens:0018", 300.5, clock) == 1500.5
    entry = counters.entry(conn, "tokens:0018")
    assert entry is not None and entry.window_start == clock.now() - timedelta(minutes=5)
    counters.reset(conn, "tokens:0018", clock)
    entry = counters.entry(conn, "tokens:0018")
    assert entry is not None and (entry.value, entry.window_start) == (0.0, clock.now())
    counters.reset(conn, "breaker:gh", clock)
    assert counters.get(conn, "breaker:gh") == 0.0
