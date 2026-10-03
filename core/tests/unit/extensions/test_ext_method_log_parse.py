import json
from datetime import datetime, timezone

import pytest
from method_world import call, error_of, output_of, request

from tightrein.domain.enums import ExtensionPoint
from tightrein.extensions.methods.log_parse import json_lines, regex
from tightrein.extensions.methods.runtime import MethodContext

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
POINT = ExtensionPoint.LOG_PARSE


def chunk(text, stream="app.log", start=0, modified="2026-10-05T02:59:00Z"):
    return {"stream": stream, "text": text, "startPosition": start, "endPosition": start + len(text.encode("utf-8")),
            "modifiedAt": modified}


def parse(module, tmp_path, chunks, options=None, state=None):
    document = request(POINT, workspace=tmp_path, options=options or {}, input={"chunks": chunks, "state": state})
    return output_of(module, document, MethodContext(now=lambda: NOW))


def brief(entry):
    return (entry["position"], entry["occurredAt"], entry["level"], entry["rawLevel"], entry["category"],
            entry["eventId"], entry["message"])


JSON_TEXT = "\n".join([
    json.dumps({"timestamp": "2026-10-05T02:10:00Z", "level": "Error", "message": "查询失败", "logger": "orders",
                "eventId": 7, "error": {"type": "TimeoutError", "message": "30 秒超时"}}, ensure_ascii=False),
    "",
    json.dumps({"timestamp": "2026-10-05 11:11:00", "level": "warn", "message": "slow"}),
    "not json",
    json.dumps({"timestamp": 1791166320000, "level": "INFO", "message": {"k": 1}}),
    json.dumps({"timestamp": "yesterday", "level": "error", "message": "bad time"}),
    json.dumps({"timestamp": "2026-10-05T02:13:00Z", "level": "notice-ish", "message": "unknown level"}),
    json.dumps(["2026-10-05T02:14:00Z", "error"]),
    "",
])


def test_json_lines_with_nested_fields_and_local_time(tmp_path):
    options = {"categoryField": "logger", "eventIdField": "eventId", "exceptionTypeField": "error.type",
               "exceptionMessageField": "error.message", "timezone": "Asia/Tokyo"}
    output = parse(json_lines, tmp_path, [chunk(JSON_TEXT, start=100)], options, state={"kept": 1})
    entries = output["entries"]
    assert [brief(entry) for entry in entries] == [
        (100, "2026-10-05T02:10:00Z", "error", "Error", "orders", 7, "查询失败"),
        (100 + len(JSON_TEXT.split("\n")[0].encode("utf-8")) + 2, "2026-10-05T02:11:00Z", "warning", "warn", None,
         None, "slow"),
        (entries[2]["position"], "2026-10-05T02:12:00Z", "information", "INFO", None, None, '{"k": 1}'),
    ]
    assert entries[0]["exception"] == {"type": "TimeoutError", "message": "30 秒超时"}
    assert (entries[0]["frames"], entries[1]["exception"], entries[1]["localTime"]) == ([], None, "2026-10-05 11:11:00")
    assert entries[0]["raw"] == JSON_TEXT.split("\n")[0]
    assert (output["unparsed"], output["state"]) == (4, {"kept": 1})


def test_json_lines_level_mapping_comes_from_the_options(tmp_path):
    line = json.dumps({"time": 1791166320, "severity": 50, "msg": "boom"})
    options = {"timeField": "time", "levelField": "severity", "messageField": "msg", "levels": {"50": "error"}}
    output = parse(json_lines, tmp_path, [chunk(line + "\n")], options)
    assert [brief(entry) for entry in output["entries"]] == [(0, "2026-10-05T02:12:00Z", "error", "50", None, None,
                                                               "boom")]


def test_json_lines_keeps_a_text_event_id(tmp_path):
    lines = [json.dumps({"at": "2026-09-28T11:30:00+08:00", "status": "失败", "summary": "采集失败",
                         "run": "2026-09-28 run02_1130"}, ensure_ascii=False),
             json.dumps({"at": "2026-09-28T11:31:00+08:00", "status": "失败", "summary": "x", "run": "42"}),
             json.dumps({"at": "2026-09-28T11:32:00+08:00", "status": "失败", "summary": "y", "run": " "})]
    options = {"timeField": "at", "levelField": "status", "messageField": "summary", "eventIdField": "run",
               "levels": {"失败": "error"}}
    output = parse(json_lines, tmp_path, [chunk("\n".join(lines) + "\n")], options)
    assert [entry["eventId"] for entry in output["entries"]] == ["2026-09-28 run02_1130", 42, None]


def test_an_unknown_timezone_is_invalid_input(tmp_path):
    document = request(POINT, workspace=tmp_path, options={"timezone": "Mars/Base"},
                       input={"chunks": [], "state": None})
    assert error_of(json_lines, document) == ("invalid-input", "不认识的时区：Mars/Base")


PATTERN = r"^(?P<time>\d{2}:\d{2}:\d{2}) (?P<level>[A-Z]+) (?P<category>[\w.]+)\[(?P<eventId>\d+)\]: (?P<message>.*)$"
TIME_ONLY = {"pattern": PATTERN, "timeFormat": "%H:%M:%S", "timezone": "Asia/Tokyo"}
TEXT = """    at leading continuation
23:58:00 ERROR orders.worker[3]: 保存失败
    Traceback line 1
    Traceback line 2
23:59:30 NOTE orders.worker[4]: unknown level
00:01:00 WARN orders.api[0]: 重试
"""


def test_time_only_entries_get_their_date_and_cross_midnight(tmp_path):
    output = parse(regex, tmp_path, [chunk(TEXT, modified="2026-10-04T15:02:00Z")], TIME_ONLY)
    entries = output["entries"]
    assert [brief(entry) for entry in entries] == [
        (28, "2026-10-04T14:58:00Z", "error", "ERROR", "orders.worker", 3, "保存失败"),
        (entries[1]["position"], "2026-10-04T15:01:00Z", "warning", "WARN", "orders.api", 0, "重试"),
    ]
    assert entries[0]["raw"] == "23:58:00 ERROR orders.worker[3]: 保存失败\n    Traceback line 1\n    Traceback line 2"
    assert entries[0]["localTime"] == "23:58:00"
    assert output["unparsed"] == 2
    assert output["state"] == {"app.log": "2026-10-05T00:01:00"}


def test_the_state_carries_the_date_to_the_next_chunk(tmp_path):
    later = "00:30:00 ERROR orders.api[0]: next\n"
    output = parse(regex, tmp_path, [chunk(later, modified=None)], TIME_ONLY, state={"app.log": "2026-10-05T00:01:00"})
    assert output["entries"][0]["occurredAt"] == "2026-10-04T15:30:00Z"
    fresh = parse(regex, tmp_path, [chunk("11:50:00 ERROR orders.api[0]: fresh\n", modified=None)], TIME_ONLY)
    assert fresh["entries"][0]["occurredAt"] == "2026-10-05T02:50:00Z"


def test_dated_formats_and_single_line_mode(tmp_path):
    options = {"pattern": r"^(?P<time>\S+ \S+) (?P<level>\w+) (?P<message>.*)$", "timeFormat": "%Y-%m-%d %H:%M:%S",
               "multiline": False}
    text = "2026-10-05 02:00:00 fatal disk full\n  detail\n"
    output = parse(regex, tmp_path, [chunk(text)], options, state={"app.log": "2026-10-01T00:00:00"})
    assert [brief(entry) for entry in output["entries"]] == [
        (0, "2026-10-05T02:00:00Z", "critical", "fatal", None, None, "disk full")]
    assert (output["unparsed"], output["state"]) == (1, {"app.log": "2026-10-01T00:00:00"})


@pytest.mark.parametrize(("pattern", "message"), [
    (r"^(?P<time>\S+) (?P<message>.*)$", "options.pattern 缺少命名分组：level"),
    (r"^(?P<time>[)", "options.pattern 不是合法的正则："),
])
def test_patterns_must_compile_and_name_the_required_groups(tmp_path, pattern, message):
    document = request(POINT, workspace=tmp_path, options={"pattern": pattern}, input={"chunks": [], "state": None})
    response = call(regex, document)
    assert response["error"]["code"] == "invalid-input" and response["error"]["message"].startswith(message)
