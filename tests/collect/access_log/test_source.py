import json
from datetime import UTC, datetime, timedelta

import pytest

from tightrein.collect.access_log import source
from tightrein.collect.common.source import SourceInvalid, SourceMisconfigured, SourceStatus, SourceUnavailable
from tightrein.protocol.http import HttpResponse
from tightrein.protocol.process import Command, Outcome
from tightrein.store.tables import state

QUERY_RANGE = "/loki/api/v1/query_range"
SITES = {"loki": {"url": "https://logs.example.test"}}


def line(method, route, status, duration):
    return json.dumps({"method": method, "route": route, "status": status, "durationMs": duration})


def page(respond, lines):
    start = int(datetime(2026, 10, 5, 2, 0, tzinfo=UTC).timestamp()) * 10 ** 9
    return respond({"data": {"resultType": "streams", "result": [
        {"stream": {"job": "nginx"}, "values": [[str(start + index), text] for index, text in enumerate(lines)]}]}})


def runtime(source_runtime, **changes):
    controls = {source.SOURCE: {"query": '{job="nginx"}', "minRequests": 3, **changes}}
    return source_runtime(modules={source.SOURCE: {"method": "loki"}}, controls=controls, sites=SITES)


def test_the_first_window_sets_the_baseline_and_later_regressions_become_signals(source_runtime, routes, respond,
                                                                               source_conn, source_clock):
    current = runtime(source_runtime)
    fast = [line("GET", "/api/orders", 200, 20)] * 4
    first = source.collect(current, routes([(QUERY_RANGE, {}, page(respond, fast))]))
    assert first.signals == [] and "还没有基线，本次统计作为基线" in first.notes
    for key, value in first.state.items():
        state.put(source_conn, key, value, source_clock)
    source_clock.advance(timedelta(hours=1))  # 同一时刻再读是空窗口(起点接着上次终点)
    slow = [line("GET", "/api/orders", 500, 90)] * 4
    second = source.collect(current, routes([(QUERY_RANGE, {}, page(respond, slow))]))
    assert sorted(signal.check_type for signal in second.signals) == ["error_rate", "latency"]
    signal = second.signals[0]
    assert (signal.location, signal.evidence["sourceName"]) == ("GET /api/orders", "access_log")
    assert second.coverage == ["access_log"]
    assert second.state["collect.access_log"]["baseline"]["GET /api/orders"]["requests"] == 4


def test_a_failed_read_raises_without_moving_the_cursor(source_runtime, routes):
    with pytest.raises(SourceUnavailable):
        source.collect(runtime(source_runtime), routes([(QUERY_RANGE, {}, HttpResponse(503, b"{}"))]))
    with pytest.raises(SourceMisconfigured, match="query"):
        source.collect(runtime(source_runtime, query=None), routes([]))
    assert source.collect(source_runtime(), routes([])).status is SourceStatus.SKIPPED


def test_a_truncated_read_stops_where_it_reached(source_runtime, routes, respond):
    lines = [line("GET", "/a", 200, 5)] * 3
    result = source.collect(runtime(source_runtime, limit=2, loki={"pageSize": 2}),
                            routes([(QUERY_RANGE, {}, page(respond, lines))]))
    assert result.state["collect.access_log"]["until"] == "2026-10-05T02:00:00Z"
    assert any("条数上限" in note for note in result.notes)


class ScriptRunner:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.commands: list[Command] = []

    def run(self, command: Command) -> Outcome:
        self.commands.append(command)
        return Outcome(0, self.stdout, "", 1, None, None)


def custom(source_runtime, stdout):
    runner = ScriptRunner(stdout)
    return source_runtime(modules={source.SOURCE: {"status": "custom", "script": "scripts/access_from_db.py",
                                                   "guide": "02-database.md", "reason": "访问记录在数据库里"}},
                          controls={source.SOURCE: {"minRequests": 1}}, runner=runner), runner


def test_a_project_script_can_hand_over_parsed_requests(source_runtime):
    requests = [{"method": "get", "route": "/api/x?y=1", "status": 200, "durationMs": 12}] * 2
    current, runner = custom(source_runtime, json.dumps({"requests": requests, "truncated": False}))
    result = source.collect(current)
    assert result.status is SourceStatus.DONE and result.read == 2
    assert "GET /api/x" in result.state["collect.access_log"]["baseline"]
    [command] = runner.commands
    assert command.argv[-1] == "scripts/access_from_db.py"
    assert json.loads(command.stdin)["window"] == {"since": "2026-10-04T03:00:00Z", "until": "2026-10-05T03:00:00Z"}


def test_a_project_script_can_hand_over_lines_and_bad_output_is_rejected(source_runtime):
    current, _ = custom(source_runtime, json.dumps({"lines": [line("POST", "/a", 201, 3)], "truncated": True}))
    result = source.collect(current)
    assert result.read == 1 and any("条数上限" in note for note in result.notes)
    broken, _ = custom(source_runtime, json.dumps({"rows": []}))
    with pytest.raises(SourceInvalid, match="output.schema.json"):
        source.collect(broken)
    not_json, _ = custom(source_runtime, "oops")
    with pytest.raises(SourceInvalid, match="不是 JSON"):
        source.collect(not_json)
