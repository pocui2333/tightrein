from datetime import UTC, datetime

import pytest

from tightrein.collect.common.source import SourceMisconfigured
from tightrein.collect.platform_errors.log_parse import regex
from tightrein.collect.platform_errors.log_platform.chunks import Chunk

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
LEVELS = {"error": "error", "warn": "warning", "fatal": "critical"}
PATTERN = (r"^(?P<time>\d{2}:\d{2}:\d{2}) (?P<level>[A-Z]+) (?P<category>[\w.]+)\[(?P<eventId>\d+)\]: "
           r"(?P<message>.*)$")
TIME_ONLY = {"pattern": PATTERN, "timeFormat": "%H:%M:%S", "timezone": "Asia/Tokyo", "levels": LEVELS,
             "multiline": True, "rolloverToleranceMinutes": 30}
TEXT = """    at leading continuation
23:58:00 ERROR orders.worker[3]: 保存失败
    Traceback line 1
    Traceback line 2
23:59:30 NOTE orders.worker[4]: unknown level
00:01:00 WARN orders.api[0]: 重试
"""


def chunk(text, modified, stream="app.log"):
    return Chunk(stream, text, 0, modified)


def brief(entry):
    return (entry.position, entry.occurred_at, entry.level, entry.raw_level, entry.category, entry.event_id,
            entry.message)


def test_time_only_entries_get_their_date_and_cross_midnight():
    result = regex.parse([chunk(TEXT, datetime(2026, 10, 4, 15, 2, tzinfo=UTC))], TIME_ONLY, None, NOW)
    entries = result.entries
    assert [brief(entry) for entry in entries] == [
        (28, datetime(2026, 10, 4, 14, 58, tzinfo=UTC), "error", "ERROR", "orders.worker", 3, "保存失败"),
        (entries[1].position, datetime(2026, 10, 4, 15, 1, tzinfo=UTC), "warning", "WARN", "orders.api", 0, "重试"),
    ]
    assert entries[0].raw == "23:58:00 ERROR orders.worker[3]: 保存失败\n    Traceback line 1\n    Traceback line 2"
    assert entries[0].local_time == "23:58:00"
    assert result.unparsed == 2
    assert result.state == {"app.log": "2026-10-05T00:01:00"}


def test_the_state_carries_the_date_to_the_next_chunk():
    later = regex.parse([chunk("00:30:00 ERROR orders.api[0]: next\n", None)], TIME_ONLY,
                        {"app.log": "2026-10-05T00:01:00"}, NOW)
    assert later.entries[0].occurred_at == datetime(2026, 10, 4, 15, 30, tzinfo=UTC)
    fresh = regex.parse([chunk("11:50:00 ERROR orders.api[0]: fresh\n", None)], TIME_ONLY, None, NOW)
    assert fresh.entries[0].occurred_at == datetime(2026, 10, 5, 2, 50, tzinfo=UTC)


def test_dated_formats_and_single_line_mode():
    options = {**TIME_ONLY, "pattern": r"^(?P<time>\S+ \S+) (?P<level>\w+) (?P<message>.*)$",
               "timeFormat": "%Y-%m-%d %H:%M:%S", "timezone": "UTC", "multiline": False}
    text = "2026-10-05 02:00:00 fatal disk full\n  detail\n"
    result = regex.parse([chunk(text, None)], options, {"app.log": "2026-10-01T00:00:00"}, NOW)
    assert [brief(entry) for entry in result.entries] == [
        (0, datetime(2026, 10, 5, 2, 0, tzinfo=UTC), "critical", "fatal", None, None, "disk full")]
    assert (result.unparsed, result.state) == (1, {"app.log": "2026-10-01T00:00:00"})


@pytest.mark.parametrize(("pattern", "message"), [
    (r"^(?P<time>\S+) (?P<message>.*)$", "缺少命名分组：level"),
    (r"^(?P<time>[)", "不是合法的正则"),
])
def test_patterns_must_compile_and_name_the_required_groups(pattern, message):
    with pytest.raises(SourceMisconfigured, match=message):
        regex.parse([], {**TIME_ONLY, "pattern": pattern}, None, NOW)
