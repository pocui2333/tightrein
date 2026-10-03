from datetime import date, datetime, timedelta, timezone

import pytest

from tightrein.domain.clock import (
    FixedClock,
    SystemClock,
    format_iso,
    is_workday,
    local_date,
    parse_iso,
    workdays_between,
)


def test_fixed_clock_returns_given_time_in_utc():
    clock = FixedClock(datetime(2026, 9, 29, 2, 15, 3, tzinfo=timezone.utc))
    assert clock.now() == datetime(2026, 9, 29, 2, 15, 3, tzinfo=timezone.utc)


def test_fixed_clock_rejects_naive_datetime():
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 9, 29, 2, 15, 3))


def test_fixed_clock_advance():
    clock = FixedClock(datetime(2026, 9, 29, tzinfo=timezone.utc))
    clock.advance(timedelta(hours=1))
    assert clock.now().hour == 1


def test_system_clock_is_utc():
    assert SystemClock().now().tzinfo == timezone.utc


def test_format_iso_drops_microseconds_and_uses_z():
    value = datetime(2026, 9, 29, 2, 15, 3, 999, tzinfo=timezone.utc)
    assert format_iso(value) == "2026-09-29T02:15:03Z"


def test_format_iso_converts_other_timezones_to_utc():
    jst = timezone(timedelta(hours=9))
    assert format_iso(datetime(2026, 9, 29, 11, 0, 0, tzinfo=jst)) == "2026-09-29T02:00:00Z"


def test_parse_iso_round_trip():
    assert format_iso(parse_iso("2026-09-29T02:15:03Z")) == "2026-09-29T02:15:03Z"


def test_parse_iso_rejects_missing_timezone():
    with pytest.raises(ValueError):
        parse_iso("2026-09-29T02:15:03")


def test_is_workday_excludes_weekend_and_non_working_days():
    assert is_workday(date(2026, 9, 29), frozenset())          # 周二
    assert not is_workday(date(2026, 10, 3), frozenset())      # 周六
    assert not is_workday(date(2026, 9, 29), frozenset({date(2026, 9, 29)}))


def test_workdays_between_counts_full_workdays_after_start():
    # 周五到下周二：跨过周末，周一、周二计 2 个工作日
    assert workdays_between(date(2026, 10, 2), date(2026, 10, 6), frozenset()) == 2
    assert workdays_between(date(2026, 10, 6), date(2026, 10, 6), frozenset()) == 0


def test_local_date_uses_the_given_zone():
    tokyo = timezone(timedelta(hours=9))
    assert local_date(datetime(2026, 10, 5, 14, 59, tzinfo=timezone.utc), tokyo) == date(2026, 10, 5)
    assert local_date(datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc), tokyo) == date(2026, 10, 6)
    new_york = timezone(timedelta(hours=-5))
    assert local_date(datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc), new_york) == date(2026, 10, 4)


def test_local_date_defaults_to_the_system_zone():
    value = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
    assert local_date(value) == value.astimezone().date()


def test_local_date_rejects_naive_datetime():
    with pytest.raises(ValueError):
        local_date(datetime(2026, 10, 5, 3, 0))
