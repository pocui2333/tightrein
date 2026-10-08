import json
from datetime import UTC, datetime

import pytest

from tightrein.collect.common.source import SourceMisconfigured, SourceStatus
from tightrein.collect.platform_errors import source
from tightrein.protocol.http import HttpResponse

TOKEN = "sentry-token-0123456789abcdef"
ISSUES = "/api/0/organizations/acme/issues/"
QUERY_RANGE = "/loki/api/v1/query_range"
SITES = {"sentry": {"url": "https://sentry.example.test", "organization": "acme"},
         "loki": {"url": "https://logs.example.test"}}
ISSUE = {"id": "4512", "title": "TypeError: x is undefined", "culprit": "render(app)", "level": "error", "count": 7,
         "userCount": 3, "firstSeen": "2026-10-04T01:00:00Z", "lastSeen": "2026-10-05T02:30:00Z",
         "metadata": {"type": "TypeError", "value": "x is undefined"}}
EVENT = {"platform": "javascript", "tags": [{"key": "url", "value": "https://demo.example.com/orders?token=abc"}],
         "entries": [{"type": "exception", "data": {"values": [{"stacktrace": {"frames": [
             {"filename": "src/app.js", "function": "render", "lineNo": 12, "inApp": True}]}}]}}]}


def log_line(at, level, message):
    return json.dumps({"timestamp": at, "level": level, "message": message}, ensure_ascii=False)


def loki_page(respond, lines):
    start = int(datetime(2026, 10, 5, 2, 0, tzinfo=UTC).timestamp()) * 10 ** 9
    return respond({"data": {"resultType": "streams", "result": [
        {"stream": {"app": "api"}, "values": [[str(start + index * 60 * 10 ** 9), line]
                                              for index, line in enumerate(lines)]}]}})


def sentry_routes(respond):
    return [(ISSUES, {}, respond([ISSUE])), (f"{ISSUES}4512/events/latest/", {}, respond(EVENT))]


def make(source_runtime, method="sentry+loki", **controls):
    section = {"logQuery": '{app="api"} |= "error"', **controls}
    return source_runtime(modules={source.SOURCE: {"method": method}}, controls={source.SOURCE: section},
                          sites=SITES, secrets={"sentry.token": TOKEN})


def test_disabled_and_misconfigured(source_runtime, routes):
    disabled = source.collect(source_runtime(), routes([]))
    assert disabled.status is SourceStatus.SKIPPED and disabled.reason.startswith("未启用")
    with pytest.raises(SourceMisconfigured, match="没有这个平台方法"):
        source.collect(make(source_runtime, method="elastic"), routes([]))
    with pytest.raises(SourceMisconfigured, match="method"):
        source.collect(make(source_runtime, method=None), routes([]))


def test_both_platforms_are_read_with_their_own_cursors(source_runtime, routes, respond):
    lines = [log_line("2026-10-05T02:40:00Z", "error", "查询失败"), log_line("2026-10-05T02:41:00Z", "info", "ok"),
             "not json"]
    transport = routes([*sentry_routes(respond), (QUERY_RANGE, {}, loki_page(respond, lines))])
    result = source.collect(make(source_runtime), transport)
    assert result.status is SourceStatus.DONE and sorted(result.coverage) == ["error_tracking", "log_platform"]
    tracked, logged = sorted(result.signals, key=lambda signal: signal.evidence["sourceName"])
    assert (tracked.group_key, tracked.location, tracked.check_type) == ("sentry:acme/4512", "src/app.js:render",
                                                                         "error")
    assert "abc" not in tracked.evidence["url"]
    assert (logged.location, logged.message, logged.group_key) == ("(无类别)", "查询失败", None)
    assert result.state["collect.platform_errors:error_tracking"] == {"until": "2026-10-05T03:00:00Z"}
    assert result.state["collect.platform_errors:log_platform"] == {"until": "2026-10-05T03:00:00Z", "parseState": {}}
    assert result.metrics.produced == {"issues": 1, "logEntries": 2, "selectedEntries": 1, "unparsedLines": 1,
                                       "signals": 2}
    assert result.window == ("2026-10-04T03:00:00Z", "2026-10-05T03:00:00Z") and result.read == 3
    issues = next(index for index, sent in enumerate(transport.sent) if sent.url.split("?")[0].endswith(ISSUES))
    assert transport.query(issues)["start"] == ["2026-10-04T03:00:00Z"]


def test_one_platform_failing_keeps_the_other_and_its_cursor(source_runtime, routes, respond):
    transport = routes([*sentry_routes(respond), (QUERY_RANGE, {}, HttpResponse(503, b"{}"))])
    result = source.collect(make(source_runtime), transport)
    assert result.status is SourceStatus.PARTIAL and len(result.signals) == 1
    assert list(result.state) == ["collect.platform_errors:error_tracking"]
    assert result.coverage == ["error_tracking"] and result.reason.startswith("log_platform 失败：unavailable")
    both = routes([(ISSUES, {}, HttpResponse(500, b"{}")), (QUERY_RANGE, {}, HttpResponse(503, b"{}"))])
    failed = source.collect(make(source_runtime), both)
    assert failed.status is SourceStatus.FAILED and failed.state == {} and "error_tracking 失败" in failed.reason


def test_a_truncated_log_read_continues_from_the_last_line_read(source_runtime, routes, respond):
    lines = [log_line("2026-10-05T02:00:00Z", "error", "a"), log_line("2026-10-05T02:01:00Z", "error", "b")]
    runtime = make(source_runtime, method="loki", logLimit=2, loki={"pageSize": 2})
    result = source.collect(runtime, routes([(QUERY_RANGE, {}, loki_page(respond, lines))]))
    assert result.state["collect.platform_errors:log_platform"]["until"] == "2026-10-05T02:01:00Z"
    assert any("logLimit" in note for note in result.notes)


def test_a_missing_log_query_fails_only_the_log_platform(source_runtime, routes, respond):
    runtime = make(source_runtime, logQuery=None)
    result = source.collect(runtime, routes(sentry_routes(respond)))
    assert result.status is SourceStatus.PARTIAL and "logQuery" in result.reason
