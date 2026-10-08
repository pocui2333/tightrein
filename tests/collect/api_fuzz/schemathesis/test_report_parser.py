import json

from tightrein.collect.api_fuzz.schemathesis import report_parser

TESTED = (("POST", "/api/Order/Query"), ("GET", "/api/Company/{id}"), ("GET", "/api/Order/List"),
          ("GET", "/api/Order/{id}"), ("GET", "/api/Report/Export"), ("GET", "/api/User/List"))


def test_complete_report(tmp_path, write_events):
    report = report_parser.parse(write_events(tmp_path / "events.ndjson"), 200)
    assert report.complete and report.unknown == 0 and report.errors == ()
    assert report.seed == 20261005 and report.finished_at == 1790721554.417758
    assert report.tested == TESTED
    assert len(report.failures) == 11


def test_server_error_details(tmp_path, write_events):
    report = report_parser.parse(write_events(tmp_path / "events.ndjson"), 200)
    failure = next(item for item in report.failures if item.check == "not_a_server_error")
    assert failure.operation == ("POST", "/api/Order/Query") and failure.title == "Server error"
    assert (failure.case_id, failure.case.method, failure.case.path_template) == ("erhKg8", "POST", "/api/Order/Query")
    assert failure.case.body == {"pageSize": 0} and failure.case.media_type == "application/json"
    interaction = failure.interaction
    assert (interaction.method, interaction.uri, interaction.status) == (
        "POST", "http://127.0.0.1:18282/api/Order/Query", 500)
    # 响应体在报告中是 base64
    assert json.loads(interaction.body) == {"error": "Value cannot be negative or zero. (Parameter 'pageSize')"}
    assert interaction.timestamp == 1790721552.906347


def test_truncated_report_is_incomplete(tmp_path, write_events):
    report = report_parser.parse(write_events(tmp_path / "events.ndjson", drop="EngineFinished"), 200)
    assert not report.complete and len(report.failures) == 11


def test_unknown_events_and_broken_lines_are_counted(tmp_path, write_events, recorded_events):
    path = write_events(tmp_path / "events.ndjson", [{"Mystery": {}}, *recorded_events])
    path.write_text(path.read_text(encoding="utf-8") + '{"ScenarioFinished": {"recorder"\n', encoding="utf-8")
    report = report_parser.parse(path, 200)
    assert report.complete and report.unknown == 2


def test_non_fatal_errors_and_missing_file(tmp_path, write_events, recorded_events):
    error = {"NonFatalError": {"label": "GET /api/Order/{id}", "value": {"type": "InvalidSchema", "message": "bad"}}}
    report = report_parser.parse(write_events(tmp_path / "events.ndjson", [error, *recorded_events]), 200)
    assert report.errors == ("GET /api/Order/{id}：InvalidSchema：bad",)
    assert report_parser.parse(tmp_path / "none.ndjson", 200) == report_parser.Report(False)


def test_the_failing_operation_comes_from_the_failure_info():
    """有状态测试的一个场景跨多个操作：失败所属的操作以 failure_info 中的为准。"""
    scenario = {"ScenarioFinished": {"phase": "stateful", "status": "failure", "recorder": {
        "label": "Stateful tests",
        "cases": {"c1": {"value": {"method": "get", "path": "/api/items/{id}"}}},
        "checks": {"c1": [{"name": "not_a_server_error", "status": "failure",
                           "failure_info": {"failure": {"operation": "GET /api/items/{id}", "title": "Server error"}}}]},
        "interactions": {}}}}
    report = report_parser.parse_lines([json.dumps(scenario), json.dumps({"EngineFinished": {}})], 200)
    assert [(item.operation, item.phase) for item in report.failures] == [(("GET", "/api/items/{id}"), "stateful")]
    assert report.tested == ()
