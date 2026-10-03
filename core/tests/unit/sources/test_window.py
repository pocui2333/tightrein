from datetime import timedelta

from probe_world import NOW, open_db

from tightrein.sources.common import window
from tightrein.store.repos import source_cursors


def test_first_read_looks_back_and_later_reads_continue_from_the_saved_end(tmp_path):
    conn = open_db(tmp_path)
    first = window.plan(conn, "alerts", NOW, 24)
    assert (first.since, first.until, first.previous) == (NOW - timedelta(hours=24), NOW, None)
    source_cursors.save(conn, first.advance(NOW, {"baseline": {}}, {"s": 1}))
    later = window.plan(conn, "alerts", NOW + timedelta(hours=1), 24)
    assert (later.since, later.until) == (NOW, NOW + timedelta(hours=1))
    assert later.previous.cursor == {"until": "2026-10-05T03:00:00Z", "baseline": {}}
    assert later.previous.parse_state == {"s": 1}


def test_a_truncated_read_stops_where_it_reached(tmp_path):
    span = window.plan(open_db(tmp_path), "logs", NOW, 2)
    assert span.advance(NOW, until=NOW - timedelta(hours=1)).cursor["until"] == "2026-10-05T02:00:00Z"
    assert span.advance(NOW, until=NOW + timedelta(hours=1)).cursor["until"] == "2026-10-05T03:00:00Z"


def test_retention_gaps_are_reported():
    span = window.Window("platform-errors:error-tracking", NOW - timedelta(days=40), NOW)
    assert span.gap(None) is None
    assert span.gap("2026-08-01T00:00:00Z") is None
    note = span.gap("2026-09-05T03:00:00Z")
    assert note.startswith("可能漏读：platform-errors:error-tracking 在 2026-08-26T03:00:00Z 到 2026-09-05T03:00:00Z")
