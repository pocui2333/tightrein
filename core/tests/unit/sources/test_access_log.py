import json

import pytest
from platform_world import LOG_PLATFORM, PlatformClient, broken, chunk, ok
from probe_world import NOW, RELEASE, CountingRandom, make_redactor, make_target, open_db

from tightrein.domain.enums import RunStatus, Source
from tightrein.sources.access_log import parse, stats
from tightrein.sources.access_log.source import DISABLED, AccessLogDependencies, AccessLogSource
from tightrein.sources.base import ProbeOptions
from tightrein.store.repos import source_cursors


def line(method, route, status, duration):
    return json.dumps({"req": {"method": method, "path": route}, "status": status, "ms": duration})


FIELDS = {"method": "req.method", "route": "req.path", "status": "status", "durationMs": "ms"}


def test_parse_json_and_pattern_lines():
    found, unparsed = parse.parse([line("get", "/api/orders?page=2", 200, 12), "not json", ""], FIELDS, None)
    assert found == [parse.Request("GET", "/api/orders", 200, 12.0)] and unparsed == 1
    pattern = r'"(?P<method>\w+) (?P<route>\S+) HTTP/1\.1" (?P<status>\d+) (?P<durationMs>\d+)'
    found, unparsed = parse.parse(['1.2.3.4 - "POST /api/login HTTP/1.1" 500 87', "garbage"], {}, pattern)
    assert found == [parse.Request("POST", "/api/login", 500, 87.0)] and unparsed == 1


def test_compare_flags_latency_and_error_rate_regressions():
    base = {"GET /a": stats.EndpointStats(100, 50.0, 0.0), "GET /b": stats.EndpointStats(100, 40.0, 0.01)}
    now = {"GET /a": stats.EndpointStats(30, 140.0, 0.0), "GET /b": stats.EndpointStats(30, 41.0, 0.2),
           "GET /c": stats.EndpointStats(30, 900.0, 0.5)}
    found = stats.compare(now, base, min_requests=20, latency_ratio=2, error_rate_delta=0.05)
    assert [(item.endpoint, item.check) for item in found] == [("GET /a", stats.LATENCY), ("GET /b", stats.ERROR_RATE)]
    assert stats.compare({"GET /a": stats.EndpointStats(5, 999.0, 1.0)}, base, min_requests=20, latency_ratio=2,
                         error_rate_delta=0.05) == []
    merged = stats.update(base, now, 0.5)
    assert merged["GET /a"].p95_ms == pytest.approx(95.0) and merged["GET /c"] == now["GET /c"]


def make_source(tmp_path, make_config, client, **settings):
    config = make_config(sources={"access-log": {"query": '{job="nginx"}', "fields": FIELDS, "minRequests": 3,
                                                 **settings}})
    conn = open_db(tmp_path)
    return AccessLogSource(AccessLogDependencies(config, client, conn, make_redactor(), lambda at: RELEASE,
                                                 CountingRandom())), conn


def read(tmp_path, found):
    return found.run(make_target(tmp_path, "access-log"), None, ProbeOptions())


def platform(lines):
    return ok(LOG_PLATFORM, {"chunks": [chunk("{job=\"nginx\"}", lines)], "truncated": False, "oldestAvailable": None})


def test_disabled_without_a_query(tmp_path, make_config):
    found = AccessLogSource(AccessLogDependencies(make_config(), PlatformClient({LOG_PLATFORM}), open_db(tmp_path),
                                                  make_redactor(), lambda at: None))
    assert read(tmp_path, found).skipped_reason == DISABLED


def test_the_first_window_sets_the_baseline_and_later_regressions_become_signals(tmp_path, make_config):
    client = PlatformClient({LOG_PLATFORM}, log_platform=platform([line("GET", "/api/orders", 200, 20)] * 4))
    found, conn = make_source(tmp_path, make_config, client)
    first = read(tmp_path, found)
    assert first.signals == () and "还没有基线，本次统计作为基线" in first.notes
    source_cursors.save(conn, first.cursors[0])
    client.results["log_platform"] = platform([line("GET", "/api/orders", 500, 90)] * 4)
    second = read(tmp_path, found)
    checks = sorted(signal.check for signal in second.signals)
    assert checks == ["error-rate-regression", "latency-regression"]
    signal = second.signals[0]
    assert (signal.source, signal.location, signal.context["sourceName"]) == (Source.PERFORMANCE, "GET /api/orders",
                                                                              "access-log")
    assert second.coverage.sources == ("access-log",) and second.cursors[0].cursor["baseline"]["GET /api/orders"]


def test_a_failed_read_fails_without_moving_the_cursor(tmp_path, make_config):
    found, _ = make_source(tmp_path, make_config, PlatformClient({LOG_PLATFORM}, log_platform=broken(LOG_PLATFORM)))
    outcome = read(tmp_path, found)
    assert outcome.status is RunStatus.FAILED and outcome.cursors == ()
