from datetime import UTC, datetime, timedelta

from tightrein.collect.common import window
from tightrein.store.tables import state

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
LOOKBACK = 24 * 3600


def test_first_read_looks_back_and_later_reads_continue_from_the_saved_end(source_conn, source_clock):
    first = window.plan(source_conn, "collect.alerts", NOW.replace(microsecond=123), LOOKBACK)
    assert (first.since, first.until, first.previous) == (NOW - timedelta(hours=24), NOW, None)
    state.put(source_conn, first.key, first.advance(parse_state={"s": 1}, extra={"baseline": {}}), source_clock)
    later = window.plan(source_conn, "collect.alerts", NOW + timedelta(hours=1), LOOKBACK)
    assert (later.since, later.until) == (NOW, NOW + timedelta(hours=1))
    assert later.previous == {"until": "2026-10-05T03:00:00Z", "baseline": {}, "parseState": {"s": 1}}
    assert later.parse_state == {"s": 1}


def test_a_clock_set_back_does_not_invert_the_window(source_conn, source_clock):
    state.put(source_conn, "k", {"until": "2026-10-05T05:00:00Z"}, source_clock)
    span = window.plan(source_conn, "k", NOW, LOOKBACK)
    assert span.since == span.until == NOW


def test_a_truncated_read_stops_where_it_reached(source_conn):
    span = window.plan(source_conn, "logs", NOW, 2 * 3600)
    assert span.advance(reached=NOW - timedelta(hours=1))["until"] == "2026-10-05T02:00:00Z"
    assert span.advance(reached=NOW + timedelta(hours=1))["until"] == "2026-10-05T03:00:00Z"
    assert span.advance() == {"until": "2026-10-05T03:00:00Z"}


def test_retention_gaps_are_reported():
    span = window.Window("collect.platform_errors:error_tracking", NOW - timedelta(days=40), NOW, None)
    assert span.gap(None) is None
    assert span.gap(datetime(2026, 8, 1, tzinfo=UTC)) is None
    note = span.gap(window.oldest_available(NOW, 30))
    assert note.startswith("可能漏读：collect.platform_errors:error_tracking 在 2026-08-26T03:00:00Z 到 "
                           "2026-09-05T03:00:00Z")


def test_state_keys_and_platform_times():
    assert window.state_key("collect.access_log") == "collect.access_log"
    assert window.state_key("collect.platform_errors", "log_platform") == "collect.platform_errors:log_platform"
    assert window.platform_time("2026-10-05T02:10:00.123456789+08:00") == datetime(2026, 10, 4, 18, 10, tzinfo=UTC)
    assert window.platform_time("2026-10-05T02:30:00.5Z") == datetime(2026, 10, 5, 2, 30, tzinfo=UTC)
