from platform_world import (ERROR_TRACKING, LOG_PARSE, LOG_PLATFORM, PlatformClient, broken, chunk, ok)
from probe_world import NOW, RELEASE, CountingRandom, make_redactor, make_target, open_db

from tightrein.domain.enums import RunStatus, Source
from tightrein.sources.base import ProbeOptions
from tightrein.sources.platform_errors.source import DISABLED, PlatformErrorsDependencies, PlatformErrorsSource
from tightrein.store.repos import source_cursors

ISSUE = {"group": "sentry:acme/4512", "kind": "frontend", "title": "TypeError: x is undefined", "type": "TypeError",
         "message": "x is undefined", "culprit": "render(app)", "level": "error", "count": 7, "userCount": 3,
         "firstSeen": "2026-10-04T01:00:00Z", "lastSeen": "2026-10-05T02:30:00Z", "release": "web@1.2",
         "environment": "production", "permalink": "https://sentry.io/issues/4512/",
         "frames": [{"file": "node_modules/react.js", "function": "commit", "line": 9, "inApp": False},
                    {"file": "src/app.js", "function": "render", "line": 12, "inApp": True}],
         "breadcrumbs": [{"time": "2026-10-05T02:29:00Z", "category": "ui.click", "message": "保存"}],
         "url": "https://demo.example.com/orders?token=abc", "browser": "Chrome 129"}
ENTRY = {"stream": "{app=\"api\"}", "position": 0, "occurredAt": "2026-10-05T02:40:00Z", "localTime": None,
         "level": "error", "rawLevel": "ERROR", "category": "orders", "eventId": None, "message": "查询失败",
         "exception": {"type": "KeyError", "message": "'id'"}, "frames": [], "raw": "{\"msg\": \"查询失败\"}"}


def source(tmp_path, make_config, client, log_query=None):
    config = make_config(sources={"platform-errors": {"logQuery": log_query}} if log_query else None)
    conn = open_db(tmp_path)
    return PlatformErrorsSource(PlatformErrorsDependencies(config, client, conn, make_redactor("abc"),
                                                           lambda at: RELEASE, CountingRandom())), conn


def run_source(tmp_path, found):
    return found.run(make_target(tmp_path, "platform-errors"), None, ProbeOptions())


def test_disabled_without_any_platform(tmp_path, make_config):
    found, _ = source(tmp_path, make_config, PlatformClient())
    outcome = run_source(tmp_path, found)
    assert (outcome.status, outcome.skipped_reason) == (RunStatus.SKIPPED, DISABLED)


def test_error_tracking_issues_become_signals_with_the_platform_group(tmp_path, make_config):
    client = PlatformClient({ERROR_TRACKING}, error_tracking=ok(ERROR_TRACKING, {
        "issues": [ISSUE], "truncated": True, "oldestAvailable": "2026-07-01T00:00:00Z"}))
    found, conn = source(tmp_path, make_config, client)
    outcome = run_source(tmp_path, found)
    assert outcome.status is RunStatus.OK and outcome.coverage.sources == ("error-tracking",)
    [signal] = outcome.signals
    assert (signal.source, signal.check, signal.location, signal.release) == (
        Source.ERROR, "frontend-error", "src/app.js:render", RELEASE)
    assert signal.message == "TypeError: x is undefined"
    context = signal.context
    assert (context["platformGroup"], context["sourceName"], context["count"]) == ("sentry:acme/4512",
                                                                                  "error-tracking", 7)
    assert context["projectFrames"] == [{"symbol": "render", "file": "src/app.js", "line": 12}]
    assert "abc" not in context["url"] and context["breadcrumbs"][0]["message"] == "保存"
    assert any("超过条数上限" in note for note in outcome.notes)
    [(_, since, until)] = client.calls
    assert until == NOW and since < until
    [cursor] = outcome.cursors
    assert cursor.source == "platform-errors:error-tracking" and cursor.cursor == {"until": "2026-10-05T03:00:00Z"}
    assert source_cursors.get(conn, cursor.source) is None


def test_log_platform_entries_are_parsed_and_filtered_by_level(tmp_path, make_config):
    info = dict(ENTRY, level="information", exception=None, message="ok")
    client = PlatformClient({LOG_PLATFORM, LOG_PARSE},
                            log_platform=ok(LOG_PLATFORM, {"chunks": [chunk("{app=\"api\"}", ["a", "b"])],
                                                           "truncated": False, "oldestAvailable": None}),
                            log_parse=ok(LOG_PARSE, {"entries": [ENTRY, info], "state": {"x": 1}, "unparsed": 1}))
    found, _ = source(tmp_path, make_config, client, log_query='{app="api"} |= "error"')
    outcome = run_source(tmp_path, found)
    [signal] = outcome.signals
    assert (signal.check, signal.location, signal.message) == ("error", "orders", "KeyError: 'id'")
    assert signal.context["sourceName"] == "log-platform" and "platformGroup" not in signal.context
    assert outcome.stats["unparsedLines"] == 1 and outcome.coverage.sources == ("log-platform",)
    assert client.calls[0][1] == '{app="api"} |= "error"' and client.calls[0][4] == 5000
    assert outcome.cursors[0].parse_state == {"x": 1}


def test_one_platform_failing_keeps_the_other_and_its_cursor(tmp_path, make_config):
    client = PlatformClient({ERROR_TRACKING, LOG_PLATFORM, LOG_PARSE},
                            error_tracking=ok(ERROR_TRACKING, {"issues": [ISSUE], "truncated": False,
                                                               "oldestAvailable": None}),
                            log_platform=broken(LOG_PLATFORM))
    found, _ = source(tmp_path, make_config, client, log_query="{app=\"api\"}")
    outcome = run_source(tmp_path, found)
    assert outcome.status is RunStatus.PARTIAL and len(outcome.signals) == 1
    assert [cursor.source for cursor in outcome.cursors] == ["platform-errors:error-tracking"]
    assert any(note.startswith("log-platform 失败") for note in outcome.notes)
    client.results["error_tracking"] = broken(ERROR_TRACKING)
    assert run_source(tmp_path, found).status is RunStatus.FAILED


def test_a_truncated_log_read_continues_from_the_last_line_read(tmp_path, make_config):
    client = PlatformClient({LOG_PLATFORM, LOG_PARSE},
                            log_platform=ok(LOG_PLATFORM, {"chunks": [chunk("s", ["a"], "2026-10-05T01:00:00Z")],
                                                           "truncated": True, "oldestAvailable": None}),
                            log_parse=ok(LOG_PARSE, {"entries": [], "state": {}, "unparsed": 0}))
    found, _ = source(tmp_path, make_config, client, log_query="{app=\"api\"}")
    outcome = run_source(tmp_path, found)
    assert outcome.cursors[0].cursor["until"] == "2026-10-05T01:00:00Z"
