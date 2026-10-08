import json
from datetime import UTC, datetime

import pytest

from tightrein.collect.common.source import SourceUnavailable
from tightrein.collect.platform_errors.error_tracking import sentry
from tightrein.protocol import methods
from tightrein.protocol.http import HttpResponse

TOKEN = "platform-token-0123456789abcdef"
NOW = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
SINCE = datetime(2026, 10, 5, 2, 0, tzinfo=UTC)
ISSUES = "/api/0/organizations/acme/issues/"


def configured(**options):
    values = {"url": "https://sentry.io", "organization": "acme", "projects": ["api", "web"], "environment": None,
              "retentionDays": 90, "limit": 100, "breadcrumbs": 2, **options}
    return methods.Configured(methods.load("tightrein.collect.platform_errors.error_tracking", "sentry"), values,
                              TOKEN)


def issue(number, platform="python"):
    return {"id": str(number), "title": f"ValueError: bad {number}", "culprit": "app.views in run", "level": "error",
            "count": "7", "userCount": 2, "firstSeen": "2026-10-01T01:00:00.123456Z",
            "lastSeen": "2026-10-05T02:30:00.5+00:00", "permalink": f"https://sentry.io/issues/{number}/",
            "metadata": {"type": "ValueError", "value": f"bad {number}"}, "platform": platform}


def event(platform="python"):
    return {"platform": platform, "release": {"version": "1.4.0"},
            "tags": [{"key": "environment", "value": "production"}, {"key": "url", "value": "https://app/x"},
                     {"key": "browser", "value": "Chrome 128"}],
            "entries": [{"type": "exception", "data": {"values": [{"type": "ValueError", "stacktrace": {"frames": [
                {"filename": "lib/runner.py", "function": "main", "lineNo": 3, "inApp": False},
                {"filename": "app/views.py", "function": "run", "lineNo": 42, "inApp": True}]}}]}},
                        {"type": "breadcrumbs", "data": {"values": [
                            {"timestamp": f"2026-10-05T02:29:0{n}Z", "category": "ui.click", "message": f"step {n}"}
                            for n in range(4)]}}]}


def sentry_routes(respond, pages, frontend=()):
    found = []
    for number, (issues, cursor) in enumerate(pages):
        link = (f'<https://sentry.io{ISSUES}?cursor={cursor}>; rel="next"; results="true"; cursor="{cursor}"'
                if cursor else '<x>; rel="next"; results="false"; cursor="0:100:0"')
        match = {"cursor": pages[number - 1][1]} if number else {}
        found.insert(0, (ISSUES, match, respond(issues, {"Link": link})))
    for issues, _ in pages:
        for item in issues:
            found.append((f"{ISSUES}{item['id']}/events/latest/", {},
                          respond(event("javascript" if item["id"] in frontend else "python"))))
    return found


def read(transport, **options):
    return sentry.read(configured(**options), transport=transport, timeout_s=5, since=SINCE, until=NOW, now=NOW)


def test_sentry_reads_pages_of_issues_and_their_latest_events(routes, respond):
    transport = routes(sentry_routes(respond, [([issue(1), issue(2)], "0:100:0"), ([issue(3, "javascript")], None)],
                                     frontend={"3"}))
    found = read(transport)
    first = transport.query(0)
    assert (first["start"], first["end"], first["query"], first["sort"], first["project"]) == (
        ["2026-10-05T02:00:00Z"], ["2026-10-05T03:00:00Z"], ["is:unresolved"], ["date"], ["api", "web"])
    assert transport.sent[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert transport.query(1)["cursor"] == ["0:100:0"]
    assert [item.group for item in found.issues] == ["sentry:acme/1", "sentry:acme/2", "sentry:acme/3"]
    one = found.issues[0]
    assert (one.frontend, one.type, one.message, one.count, one.release) == (False, "ValueError", "bad 1", 7, "1.4.0")
    assert (one.first_seen, one.last_seen) == (datetime(2026, 10, 1, 1, tzinfo=UTC),
                                               datetime(2026, 10, 5, 2, 30, tzinfo=UTC))
    assert one.frames[0] == sentry.StackFrame("app/views.py", "run", 42, True)
    assert [crumb.message for crumb in one.breadcrumbs] == ["step 2", "step 3"]
    assert (one.url, one.browser, one.environment) == ("https://app/x", "Chrome 128", "production")
    assert found.issues[2].frontend is True
    assert found.truncated is False and found.oldest_available == datetime(2026, 7, 7, 3, 0, tzinfo=UTC)


def test_sentry_stops_at_the_limit_and_reports_truncation(routes, respond):
    transport = routes(sentry_routes(respond, [([issue(1), issue(2)], "0:100:0"), ([issue(3)], None)]))
    found = read(transport, limit=2, environment="production")
    assert [item.group for item in found.issues] == ["sentry:acme/1", "sentry:acme/2"]
    assert found.truncated is True
    assert transport.query(0)["environment"] == ["production"]


def test_a_results_false_link_ends_the_paging():
    assert sentry.next_cursor('<x>; rel="next"; results="false"; cursor="0:100:0"') is None
    assert sentry.next_cursor('<x>; rel="next"; results="true"; cursor="0:200:0"') == "0:200:0"
    assert sentry.next_cursor(None) is None


def test_a_refusal_is_unavailable_without_the_token():
    with pytest.raises(SourceUnavailable) as caught:
        read(lambda request: HttpResponse(401, json.dumps({}).encode()))
    assert "401" in caught.value.message and TOKEN not in caught.value.message
