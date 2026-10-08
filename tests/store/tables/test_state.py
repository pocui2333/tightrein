import sqlite3

from tightrein.protocol.naming import FixedClock
from tightrein.store.tables import state


def test_put_get_find_and_delete(conn: sqlite3.Connection, clock: FixedClock) -> None:
    assert state.get(conn, "collect.access_log.cursor") is None
    state.put(conn, "collect.access_log.cursor", {"file": "app.log", "offset": 10}, clock)
    state.put(conn, "collect.access_log.cursor", {"file": "app.log", "offset": 42}, clock)
    state.put(conn, "collect.alerts.last", "2026-10-07T09:00:00Z", clock)
    assert state.get(conn, "collect.access_log.cursor") == {"file": "app.log", "offset": 42}
    assert state.find(conn, "collect.access_log.") == {"collect.access_log.cursor": {"file": "app.log", "offset": 42}}
    state.delete(conn, "collect.alerts.last")
    assert state.get(conn, "collect.alerts.last") is None
