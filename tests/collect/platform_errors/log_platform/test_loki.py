import base64
from datetime import UTC, datetime

import pytest

from tightrein.collect.common.source import SourceInvalid
from tightrein.collect.platform_errors.log_platform import loki
from tightrein.protocol import methods

TOKEN = "platform-token-0123456789abcdef"
NOW = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
SINCE = datetime(2026, 10, 5, 2, 0, tzinfo=UTC)
SINCE_NS = int(SINCE.timestamp()) * 10 ** 9
UNTIL_NS = SINCE_NS + 3600 * 10 ** 9
QUERY_RANGE = "/loki/api/v1/query_range"


def configured(token=TOKEN, **options):
    values = {"url": "https://logs.example.test/", "user": "12345", "tenant": "team-a", "retentionDays": 30,
              "pageSize": 2, **options}
    return methods.Configured(methods.load("tightrein.collect.platform_errors.log_platform", "loki"), values, token)


def streams(respond, *items):
    return respond({"status": "success", "data": {"resultType": "streams", "result": [
        {"stream": labels, "values": [[str(at), line] for at, line in values]} for labels, values in items]}})


def read(transport, limit=10, **options):
    return loki.read(configured(**options), transport=transport, timeout_s=5, query='{app="api"} |= "ERROR"',
                     since=SINCE, until=NOW, limit=limit, now=NOW)


def test_loki_pages_forward_and_groups_lines_by_stream(routes, respond):
    first = streams(respond, ({"app": "api", "level": "error"}, [(SINCE_NS + 1, "a\n"), (SINCE_NS + 2, "b")]))
    second = streams(respond, ({"app": "api", "level": "error"}, [(SINCE_NS + 5, "c")]),
                     ({"app": "worker"}, [(SINCE_NS + 4, "d")]))
    transport = routes([(QUERY_RANGE, {"start": str(SINCE_NS)}, first),
                        (QUERY_RANGE, {"start": str(SINCE_NS + 3)}, second),
                        (QUERY_RANGE, {"start": str(SINCE_NS + 6)}, streams(respond))])
    found = read(transport)
    sent = transport.query(0)
    assert (sent["start"], sent["end"], sent["limit"], sent["direction"]) == (
        [str(SINCE_NS)], [str(UNTIL_NS)], ["2"], ["forward"])
    assert sent["query"] == ['{app="api"} |= "ERROR"']
    headers = transport.sent[0].headers
    assert headers["Authorization"] == "Basic " + base64.b64encode(f"12345:{TOKEN}".encode()).decode()
    assert headers["X-Scope-OrgID"] == "team-a"
    assert len(transport.sent) == 3 and found.truncated is False
    by_stream = {chunk.stream: chunk for chunk in found.chunks}
    api = by_stream['{app="api",level="error"}']
    assert api.text == "a\nb\nc\n" and (api.start_position, api.end_position) == (0, 6)
    assert by_stream['{app="worker"}'].text == "d\n"
    assert found.oldest_available == datetime(2026, 9, 5, 3, 0, tzinfo=UTC)
    assert sorted(found.lines()) == ["a", "b", "c", "d"]


def test_loki_reports_truncation_at_the_limit(routes, respond):
    page = streams(respond, ({"app": "api"}, [(SINCE_NS + 1, "a"), (SINCE_NS + 2, "b")]))
    transport = routes([(QUERY_RANGE, {}, page)])
    found = read(transport, limit=2, user=None, tenant=None, token=None)
    assert found.truncated is True and len(transport.sent) == 1
    assert found.last_time() == SINCE
    assert "Authorization" not in transport.sent[0].headers and "X-Scope-OrgID" not in transport.sent[0].headers


def test_loki_rejects_metric_results(routes, respond):
    matrix = respond({"status": "success", "data": {"resultType": "matrix", "result": []}})
    with pytest.raises(SourceInvalid, match="streams"):
        read(routes([(QUERY_RANGE, {}, matrix)]))
