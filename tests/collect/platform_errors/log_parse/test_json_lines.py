import json
from datetime import UTC, datetime

import pytest

from tightrein.collect.common.source import SourceMisconfigured
from tightrein.collect.platform_errors.log_parse import json_lines
from tightrein.collect.platform_errors.log_platform.chunks import Chunk

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
LEVELS = {"error": "error", "warn": "warning", "info": "information"}
BASE = {"timeField": "timestamp", "levelField": "level", "messageField": "message", "categoryField": None,
        "eventIdField": None, "exceptionTypeField": None, "exceptionMessageField": None, "timezone": "UTC",
        "levels": LEVELS}


def chunk(text, stream="app.log", start=0):
    return Chunk(stream, text, start, datetime(2026, 10, 5, 2, 59, tzinfo=UTC))


def brief(entry):
    return (entry.position, entry.occurred_at, entry.level, entry.raw_level, entry.category, entry.event_id,
            entry.message)


TEXT = "\n".join([
    json.dumps({"timestamp": "2026-10-05T02:10:00Z", "level": "Error", "message": "查询失败", "logger": "orders",
                "eventId": 7, "error": {"type": "TimeoutError", "message": "30 秒超时"}}, ensure_ascii=False),
    "",
    json.dumps({"timestamp": "2026-10-05 11:11:00", "level": "warn", "message": "slow"}),
    "not json",
    json.dumps({"timestamp": 1791166320000, "level": "INFO", "message": {"k": 1}}),
    json.dumps({"timestamp": "yesterday", "level": "error", "message": "bad time"}),
    json.dumps({"timestamp": True, "level": "error", "message": "bool is not a time"}),
    json.dumps({"timestamp": "2026-10-05T02:13:00Z", "level": "notice-ish", "message": "unknown level"}),
    json.dumps(["2026-10-05T02:14:00Z", "error"]),
    "",
])


def test_json_lines_with_nested_fields_and_local_time():
    options = {**BASE, "categoryField": "logger", "eventIdField": "eventId", "exceptionTypeField": "error.type",
               "exceptionMessageField": "error.message", "timezone": "Asia/Tokyo", "levels": {**LEVELS}}
    result = json_lines.parse([chunk(TEXT, start=100)], options, {"kept": 1}, NOW)
    entries = result.entries
    first_line = TEXT.split("\n")[0]
    assert [brief(entry) for entry in entries] == [
        (100, datetime(2026, 10, 5, 2, 10, tzinfo=UTC), "error", "Error", "orders", 7, "查询失败"),
        (100 + len(first_line.encode("utf-8")) + 2, datetime(2026, 10, 5, 2, 11, tzinfo=UTC), "warning", "warn",
         None, None, "slow"),
        (entries[2].position, datetime(2026, 10, 5, 2, 12, tzinfo=UTC), "information", "INFO", None, None,
         '{"k": 1}'),
    ]
    assert entries[0].exception.type == "TimeoutError" and entries[0].exception.message == "30 秒超时"
    assert (entries[0].frames, entries[1].exception, entries[1].local_time) == ((), None, "2026-10-05 11:11:00")
    assert entries[0].raw == first_line
    assert (result.unparsed, result.state) == (5, {"kept": 1})


def test_json_lines_level_mapping_comes_from_the_options():
    line = json.dumps({"time": 1791166320, "severity": 50, "msg": "boom"})
    options = {**BASE, "timeField": "time", "levelField": "severity", "messageField": "msg", "levels": {"50": "error"}}
    [entry] = json_lines.parse([chunk(line + "\n")], options, None, NOW).entries
    assert brief(entry) == (0, datetime(2026, 10, 5, 2, 12, tzinfo=UTC), "error", "50", None, None, "boom")


def test_json_lines_keeps_a_text_event_id():
    lines = [json.dumps({"at": "2026-09-28T11:30:00+08:00", "status": "失败", "summary": "采集失败",
                         "run": "2026-09-28 run02_1130"}, ensure_ascii=False),
             json.dumps({"at": "2026-09-28T11:31:00+08:00", "status": "失败", "summary": "x", "run": "42"}),
             json.dumps({"at": "2026-09-28T11:32:00+08:00", "status": "失败", "summary": "y", "run": " "})]
    options = {**BASE, "timeField": "at", "levelField": "status", "messageField": "summary", "eventIdField": "run",
               "levels": {"失败": "error"}}
    result = json_lines.parse([chunk("\n".join(lines) + "\n")], options, None, NOW)
    assert [entry.event_id for entry in result.entries] == ["2026-09-28 run02_1130", 42, None]


def test_an_unknown_timezone_is_a_configuration_error():
    with pytest.raises(SourceMisconfigured, match="Mars/Base"):
        json_lines.parse([], {**BASE, "timezone": "Mars/Base"}, None, NOW)
