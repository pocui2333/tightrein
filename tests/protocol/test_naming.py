from datetime import UTC, datetime, timedelta, timezone

import pytest

from tightrein.protocol import naming
from tightrein.protocol.naming import FileName, FixedClock, SystemClock

NOW = datetime(2026, 10, 7, 9, 30, 0, tzinfo=UTC)
TOKYO = timezone(timedelta(hours=9))


# 时间：只经注入的 Clock 取得，存储一律 UTC、去微秒、Z 结尾


def test_format_iso_drops_microseconds_and_uses_z() -> None:
    assert naming.format_iso(NOW.replace(microsecond=123456)) == "2026-10-07T09:30:00Z"


def test_format_iso_converts_other_timezones_to_utc() -> None:
    assert naming.format_iso(datetime(2026, 10, 7, 18, 30, tzinfo=TOKYO)) == "2026-10-07T09:30:00Z"


def test_parse_iso_round_trip_and_rejects_missing_timezone() -> None:
    assert naming.parse_iso("2026-10-07T09:30:00Z") == NOW
    with pytest.raises(ValueError):
        naming.parse_iso("2026-10-07T09:30:00")
    with pytest.raises(ValueError):
        naming.format_iso(datetime(2026, 10, 7))  # noqa: DTZ001 故意不带时区


def test_fixed_clock_returns_utc_rejects_naive_and_advances() -> None:
    clock = FixedClock(datetime(2026, 10, 7, 18, 30, tzinfo=TOKYO))
    assert clock.now() == NOW and clock.now().tzinfo == UTC
    clock.advance(timedelta(seconds=1))
    assert clock.now() == NOW + timedelta(seconds=1)
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 10, 7))  # noqa: DTZ001 故意不带时区


def test_system_clock_is_utc_without_microseconds() -> None:
    now = SystemClock().now()
    assert now.tzinfo == UTC and now.microsecond == 0


def test_local_date_uses_the_given_zone() -> None:
    late = datetime(2026, 10, 7, 20, 0, tzinfo=UTC)
    assert naming.local_date(late, TOKYO).isoformat() == "2026-10-08"
    assert naming.local_date(late, UTC).isoformat() == "2026-10-07"
    with pytest.raises(ValueError):
        naming.local_date(datetime(2026, 10, 7))  # noqa: DTZ001 故意不带时区


def test_compact_time_for_file_names() -> None:
    assert naming.compact_time(NOW) == "20261007T093000Z"


# 时长：数字加单位


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("500ms", 0.5), ("30s", 30), ("20m", 1200), ("2h", 7200), ("90d", 90 * 86400), ("1.5h", 5400), (" 5m ", 300)],
)
def test_durations_are_numbers_with_units(text: str, seconds: float) -> None:
    assert naming.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["30", "30 s", "1w", "m", "-5s", ""])
def test_durations_without_a_unit_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="数字加单位"):
        naming.parse_duration(text)


def test_durations_and_counts_for_display() -> None:
    assert [naming.format_duration(value) for value in (12, 42 * 60, 78 * 60, 7200, 3 * 86400)] == [
        "12s", "42m", "1h18m", "2h", "3d"]
    assert [naming.format_count(value) for value in (999, 120_000, 1_200_000, 2_000_000)] == [
        "999", "120k", "1.2M", "2M"]


# 控制键与继承


def test_control_keys_inherit_from_step_to_module_to_stage_to_default() -> None:
    assert naming.key_chain("implement.design.frontend") == [
        "implement.design.frontend", "implement.design", "implement", "*"]
    assert naming.key_chain("collect.platform_errors.log_parse")[1] == "collect.platform_errors"


@pytest.mark.parametrize("key", ["Implement.code", "implement-code", "fix.code", "implement..code", "implement.code."])
def test_bad_control_keys_are_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="控制键不合规"):
        naming.check_control_key(key)


# 文件名


def test_file_names_show_stage_module_step_round_and_content() -> None:
    assert FileName("implement.code", "handoff", "json", round=2).render() == "35-implement.code.r2-handoff.json"
    assert FileName("collect.platform_errors.log_parse", "log", "log").render() == (
        "12-collect.platform_errors.log_parse-log.log")
    assert FileName("assess.triage", "handoff", "json").render() == "21-assess.triage-handoff.json"


def test_rounds_of_one_step_sort_next_to_each_other() -> None:
    names = sorted(FileName(point, "handoff", "json", round=number).render()
                   for point, number in [("implement.review", 1), ("implement.code", 2), ("implement.code", 1)])
    assert names[:2] == ["35-implement.code.r1-handoff.json", "35-implement.code.r2-handoff.json"]


def test_content_words_come_from_the_fixed_list() -> None:
    with pytest.raises(ValueError, match="词表"):
        FileName("implement.code", "output", "json").render()


def test_steps_without_a_registered_sequence_are_rejected() -> None:
    with pytest.raises(ValueError, match="没有登记文件序号"):
        naming.step_sequence("knowledge.write")


@pytest.mark.parametrize("value", ["a/b", "a\\b", "a\0b", ".", "..", ""])
def test_segments_cannot_escape_their_directory(value: str) -> None:
    with pytest.raises(ValueError):
        naming.segment(value)


# 编号


def test_run_ids_are_precise_to_the_second() -> None:
    assert naming.run_id(NOW, "collect") == "R-20261007T093000Z-collect"
    # 同一秒的两次同类运行编号相同，由 store/tables/runs.free_id 顺延一秒
    assert naming.run_id(NOW.replace(microsecond=999_999), "collect") == naming.run_id(NOW, "collect")
    assert naming.run_id(NOW + timedelta(seconds=1), "collect") == "R-20261007T093001Z-collect"
    with pytest.raises(ValueError):
        naming.run_id(NOW, "fix")


def test_the_run_start_time_is_read_from_the_id() -> None:
    assert naming.run_started("R-20261007T093000Z-collect") == NOW
    with pytest.raises(ValueError):
        naming.run_started("0018")


def test_ids_are_zero_padded_and_their_kind_is_recognised() -> None:
    assert (naming.issue_id(18), naming.problem_id(3), naming.retro_id(3)) == ("0018", "P-0003", "0003")
    assert naming.issue_id(12345) == "12345"
    assert [naming.kind_of(value) for value in ("0018", "P-0003", "R-20261007T093000Z-collect")] == [
        "issue", "problem", "run"]
    with pytest.raises(ValueError):
        naming.issue_id(0)
    with pytest.raises(ValueError):
        naming.kind_of("18")
