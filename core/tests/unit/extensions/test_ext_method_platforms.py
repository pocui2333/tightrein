"""平台读取方法：core/sentry、core/loki、core/alertmanager。HTTP 与钥匙串都用假的传输与进程执行器，不访问网络。"""

import base64
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from method_world import error_of, output_of, request

from tightrein.domain.enums import ExtensionErrorCode, ExtensionPoint
from tightrein.extensions.invoke import ProcessOutcome
from tightrein.extensions.methods.alert_source import alertmanager
from tightrein.extensions.methods.error_tracking import sentry
from tightrein.extensions.methods.log_platform import loki
from tightrein.extensions.methods.runtime import MethodContext
from tightrein.sources.common.http import HttpResponse

TOKEN = "platform-token-0123456789abcdef"
NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
WINDOW = {"since": "2026-10-05T02:00:00Z", "until": "2026-10-05T03:00:00Z"}


class Keychain:
    def __init__(self, secret=TOKEN):
        self.secret = secret
        self.requests = []

    def __call__(self, sent):
        self.requests.append(sent.argv)
        if self.secret is None:
            return ProcessOutcome(44, b"", b"The specified item could not be found in the keychain.\n")
        return ProcessOutcome(0, f"{self.secret}\n".encode(), b"")


class Transport:
    """按 URL 路径(与可选的查询条件)返回预设响应，记录全部请求。"""

    def __init__(self, routes):
        self.routes = routes
        self.sent = []

    def __call__(self, sent):
        self.sent.append(sent)
        parts = urlsplit(sent.url)
        query = parse_qs(parts.query)
        for path, match, response in self.routes:
            if parts.path == path and all(query.get(key) == [value] for key, value in match.items()):
                return response
        return HttpResponse(404, b"{}")


def ok(body, headers=None):
    return HttpResponse(200, json.dumps(body).encode("utf-8"), headers or {})


def context(transport, keychain=None):
    return MethodContext(runner=keychain or Keychain(), transport=transport, now=lambda: NOW)


# core/sentry

ISSUES = "/api/0/organizations/acme/issues/"


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


def sentry_request(tmp_path, **options):
    values = {"organization": "acme", "projects": ["api", "web"], "keychainItem": "tightrein.demo.sentry",
              "breadcrumbs": 2, **options}
    return request(ExtensionPoint.ERROR_TRACKING, workspace=tmp_path, options=values, input=WINDOW)


def sentry_routes(pages, frontend=()):
    routes = []
    for number, (issues, cursor) in enumerate(pages):
        link = (f'<https://sentry.io{ISSUES}?cursor={cursor}>; rel="next"; results="true"; cursor="{cursor}"'
                if cursor else '<x>; rel="next"; results="false"; cursor="0:100:0"')
        match = {"cursor": pages[number - 1][1]} if number else {}
        routes.insert(0, (ISSUES, match, ok(issues, {"Link": link})))
    for issues, _ in pages:
        for item in issues:
            platform = "javascript" if item["id"] in frontend else "python"
            routes.append((f"{ISSUES}{item['id']}/events/latest/", {}, ok(event(platform))))
    return routes


def test_sentry_reads_pages_of_issues_and_their_latest_events(tmp_path):
    transport = Transport(sentry_routes([([issue(1), issue(2)], "0:100:0"), ([issue(3, "javascript")], None)],
                                        frontend={"3"}))
    keychain = Keychain()
    output = output_of(sentry, sentry_request(tmp_path), context(transport, keychain))
    assert keychain.requests[0] == ("security", "find-generic-password", "-s", "tightrein.demo.sentry", "-w")
    first = parse_qs(urlsplit(transport.sent[0].url).query)
    assert (first["start"], first["end"], first["query"], first["project"]) == (
        ["2026-10-05T02:00:00Z"], ["2026-10-05T03:00:00Z"], ["is:unresolved"], ["api", "web"])
    assert transport.sent[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert parse_qs(urlsplit(transport.sent[1].url).query)["cursor"] == ["0:100:0"]
    assert [item["group"] for item in output["issues"]] == ["sentry:acme/1", "sentry:acme/2", "sentry:acme/3"]
    one = output["issues"][0]
    assert (one["kind"], one["type"], one["message"], one["count"], one["release"]) == (
        "backend", "ValueError", "bad 1", 7, "1.4.0")
    assert (one["firstSeen"], one["lastSeen"]) == ("2026-10-01T01:00:00Z", "2026-10-05T02:30:00Z")
    assert one["frames"][0] == {"file": "app/views.py", "function": "run", "line": 42, "inApp": True}
    assert [crumb["message"] for crumb in one["breadcrumbs"]] == ["step 2", "step 3"]
    assert (one["url"], one["browser"], one["environment"]) == ("https://app/x", "Chrome 128", "production")
    assert output["issues"][2]["kind"] == "frontend"
    assert output["truncated"] is False and output["oldestAvailable"] == "2026-07-07T03:00:00Z"
    assert TOKEN not in json.dumps(output)


def test_sentry_stops_at_the_limit_and_reports_truncation(tmp_path):
    transport = Transport(sentry_routes([([issue(1), issue(2)], "0:100:0"), ([issue(3)], None)]))
    output = output_of(sentry, sentry_request(tmp_path, limit=2, environment="production"), context(transport))
    assert [item["group"] for item in output["issues"]] == ["sentry:acme/1", "sentry:acme/2"]
    assert output["truncated"] is True
    assert parse_qs(urlsplit(transport.sent[0].url).query)["environment"] == ["production"]


def test_sentry_without_the_token_or_with_a_refusal_is_unavailable(tmp_path):
    code, message = error_of(sentry, sentry_request(tmp_path), context(Transport([]), Keychain(secret=None)))
    assert code == ExtensionErrorCode.SOURCE_UNAVAILABLE.value and "条目不存在" in message
    code, message = error_of(sentry, sentry_request(tmp_path),
                             context(lambda sent: HttpResponse(401, b"{}")))
    assert code == ExtensionErrorCode.SOURCE_UNAVAILABLE.value and "401" in message and TOKEN not in message


# core/loki

QUERY_RANGE = "/loki/api/v1/query_range"


def streams(*items):
    return ok({"status": "success", "data": {"resultType": "streams", "result": [
        {"stream": labels, "values": [[str(at), line] for at, line in values]} for labels, values in items]}})


def loki_request(tmp_path, limit=10, **options):
    values = {"url": "https://logs.example.test/", "keychainItem": "tightrein.demo.loki", "user": "12345",
              "tenant": "team-a", "pageSize": 2, **options}
    return request(ExtensionPoint.LOG_PLATFORM, workspace=tmp_path, options=values,
                   input={"query": '{app="api"} |= "ERROR"', **WINDOW, "limit": limit})


SINCE_NS = int(datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc).timestamp()) * 10 ** 9
UNTIL_NS = SINCE_NS + 3600 * 10 ** 9


def test_loki_pages_forward_and_groups_lines_by_stream(tmp_path):
    first = streams(({"app": "api", "level": "error"}, [(SINCE_NS + 1, "a\n"), (SINCE_NS + 2, "b")]))
    second = streams(({"app": "api", "level": "error"}, [(SINCE_NS + 5, "c")]),
                     ({"app": "worker"}, [(SINCE_NS + 4, "d")]))
    transport = Transport([(QUERY_RANGE, {"start": str(SINCE_NS)}, first),
                           (QUERY_RANGE, {"start": str(SINCE_NS + 3)}, second),
                           (QUERY_RANGE, {"start": str(SINCE_NS + 6)}, streams())])
    output = output_of(loki, loki_request(tmp_path), context(transport))
    sent = parse_qs(urlsplit(transport.sent[0].url).query)
    assert (sent["start"], sent["end"], sent["limit"], sent["direction"]) == (
        [str(SINCE_NS)], [str(UNTIL_NS)], ["2"], ["forward"])
    assert sent["query"] == ['{app="api"} |= "ERROR"']
    headers = transport.sent[0].headers
    assert headers["Authorization"] == "Basic " + base64.b64encode(f"12345:{TOKEN}".encode()).decode()
    assert headers["X-Scope-OrgID"] == "team-a"
    assert len(transport.sent) == 3 and output["truncated"] is False
    by_stream = {chunk["stream"]: chunk for chunk in output["chunks"]}
    api = by_stream['{app="api",level="error"}']
    assert api["text"] == "a\nb\nc\n" and (api["startPosition"], api["endPosition"]) == (0, 6)
    assert by_stream['{app="worker"}']["text"] == "d\n"
    assert output["oldestAvailable"] == "2026-09-05T03:00:00Z"


def test_loki_reports_truncation_at_the_limit(tmp_path):
    page = streams(({"app": "api"}, [(SINCE_NS + 1, "a"), (SINCE_NS + 2, "b")]))
    transport = Transport([(QUERY_RANGE, {}, page)])
    output = output_of(loki, loki_request(tmp_path, limit=2, user=None, tenant=None, keychainItem=None),
                       context(transport))
    assert output["truncated"] is True and len(transport.sent) == 1
    assert "Authorization" not in transport.sent[0].headers and "X-Scope-OrgID" not in transport.sent[0].headers


def test_loki_rejects_metric_results(tmp_path):
    matrix = ok({"status": "success", "data": {"resultType": "matrix", "result": []}})
    code, message = error_of(loki, loki_request(tmp_path), context(Transport([(QUERY_RANGE, {}, matrix)])))
    assert code == ExtensionErrorCode.PARSE_FAILED.value and "streams" in message


# core/alertmanager

ALERTS = "/api/alertmanager/grafana/api/v2/alerts"


def alert_request(tmp_path, **options):
    values = {"url": "https://grafana.example.test/api/alertmanager/grafana", "keychainItem": "tightrein.demo.grafana",
              **options}
    return request(ExtensionPoint.ALERT_SOURCE, workspace=tmp_path, options=values)


def test_alertmanager_reads_firing_alerts(tmp_path):
    body = [{"fingerprint": "9f2c41", "startsAt": "2026-10-05T02:10:00.123456789+08:00",
             "labels": {"alertname": "OrdersStalled", "severity": "critical"},
             "annotations": {"summary": "订单 1 小时没有进展"}, "generatorURL": "https://grafana/alert/1"},
            {"fingerprint": "77aa", "startsAt": "2026-10-05T02:00:00Z", "labels": {}, "annotations": {}}]
    transport = Transport([(ALERTS, {"active": "true", "silenced": "false", "inhibited": "false"}, ok(body))])
    output = output_of(alertmanager, alert_request(tmp_path), context(transport))
    assert transport.sent[0].headers["Authorization"] == f"Bearer {TOKEN}"
    first, second = output["alerts"]
    assert (first["fingerprint"], first["name"], first["startsAt"]) == ("9f2c41", "OrdersStalled",
                                                                          "2026-10-04T18:10:00Z")
    assert first["labels"]["severity"] == "critical" and first["generatorUrl"] == "https://grafana/alert/1"
    assert (second["name"], second["generatorUrl"]) == ("77aa", None)


def test_alertmanager_without_a_token_and_with_a_bad_body(tmp_path):
    transport = Transport([(ALERTS, {}, ok([]))])
    assert output_of(alertmanager, alert_request(tmp_path, keychainItem=None), context(transport)) == {"alerts": []}
    assert "Authorization" not in transport.sent[0].headers
    code, _ = error_of(alertmanager, alert_request(tmp_path, keychainItem=None),
                       context(Transport([(ALERTS, {}, ok({"alerts": []}))])))
    assert code == ExtensionErrorCode.PARSE_FAILED.value
