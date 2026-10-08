import pytest

from tightrein.collect.access_log import stats
from tightrein.collect.access_log.parse import Request


def test_summaries_use_the_nearest_rank_p95_and_count_requests_without_duration():
    requests = [Request("GET", "/a", 200, float(ms)) for ms in range(1, 21)] + [Request("GET", "/a", 503, None)]
    found = stats.summarize(requests)["GET /a"]
    assert (found.requests, found.p95_ms) == (21, 19.0)
    assert found.error_rate == pytest.approx(1 / 21)
    assert stats.summarize([Request("GET", "/b", 500, None)])["GET /b"].p95_ms is None


def test_compare_flags_latency_and_error_rate_regressions():
    base = {"GET /a": stats.EndpointStats(100, 50.0, 0.0), "GET /b": stats.EndpointStats(100, 40.0, 0.01)}
    now = {"GET /a": stats.EndpointStats(30, 140.0, 0.0), "GET /b": stats.EndpointStats(30, 41.0, 0.2),
           "GET /c": stats.EndpointStats(30, 900.0, 0.5)}
    found = stats.compare(now, base, min_requests=20, latency_ratio=2, error_rate_delta=0.05)
    assert [(item.endpoint, item.check_type) for item in found] == [("GET /a", stats.LATENCY),
                                                                    ("GET /b", stats.ERROR_RATE)]
    assert found[0].describe() == "p95 耗时 140 ms，基线 50 ms"
    assert stats.compare({"GET /a": stats.EndpointStats(5, 999.0, 1.0)}, base, min_requests=20, latency_ratio=2,
                         error_rate_delta=0.05) == []
    merged = stats.update(base, now, 0.5)
    assert merged["GET /a"].p95_ms == pytest.approx(95.0) and merged["GET /c"] == now["GET /c"]
    assert stats.EndpointStats.from_json(merged["GET /a"].to_json()) == merged["GET /a"]
