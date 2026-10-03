import json

from api_fuzz_reports import events, without, write_events

from tightrein.sources.api_fuzz import report_parser

TESTED = (("POST", "/api/Order/Query"), ("GET", "/api/Company/{id}"), ("GET", "/api/Order/List"),
          ("GET", "/api/Order/{id}"), ("GET", "/api/Report/Export"), ("GET", "/api/User/List"))


def test_complete_report(tmp_path):
    report = report_parser.parse(write_events(tmp_path / "events.ndjson"), 200)
    assert report.complete and report.unknown == 0 and report.errors == ()
    assert report.seed == 20261005 and report.finished_at == 1790721554.417758
    assert report.tested == TESTED
    assert [(item.phase, item.check) for item in report.failures] == [
        ("coverage", "unsupported_method"), ("coverage", "status_code_conformance"),
        ("coverage", "response_schema_conformance"), ("coverage", "max_response_time"),
        ("coverage", "unauthorized_role_access"), ("fuzzing", "not_a_server_error"),
        ("fuzzing", "status_code_conformance"), ("fuzzing", "status_code_conformance"),
        ("fuzzing", "response_schema_conformance"), ("fuzzing", "max_response_time"),
        ("fuzzing", "unauthorized_role_access"),
    ]


def test_server_error_details(tmp_path):
    report = report_parser.parse(write_events(tmp_path / "events.ndjson"), 200)
    failure = next(item for item in report.failures if item.check == "not_a_server_error")
    assert failure.operation == ("POST", "/api/Order/Query") and failure.title == "Server error"
    assert (failure.case_id, failure.case.method, failure.case.path_template) == ("erhKg8", "POST", "/api/Order/Query")
    assert failure.case.body == {"pageSize": 0} and failure.case.media_type == "application/json"
    interaction = failure.interaction
    assert (interaction.method, interaction.uri, interaction.status) == (
        "POST", "http://127.0.0.1:18282/api/Order/Query", 500)
    assert json.loads(interaction.body) == {"error": "Value cannot be negative or zero. (Parameter 'pageSize')"}
    assert interaction.timestamp == 1790721552.906347


def test_schema_timeout_and_authorization_failures(tmp_path):
    report = report_parser.parse(write_events(tmp_path / "events.ndjson"), 200)
    by_check = {(item.phase, item.check): item for item in report.failures}
    schema = by_check[("fuzzing", "response_schema_conformance")]
    assert schema.message.splitlines()[0] == '1 is not of type "string"'
    slow = by_check[("fuzzing", "max_response_time")]
    assert slow.interaction.elapsed_ms == 1212 and slow.message.startswith("Actual: 1211.65ms")
    denied = by_check[("fuzzing", "unauthorized_role_access")]
    assert denied.message == "角色 Company 缺少能力 CanManageUsers，GET /api/User/List 却返回 200"
    assert by_check[("coverage", "status_code_conformance")].interaction.status == 403


def test_truncated_report_is_incomplete(tmp_path):
    report = report_parser.parse(write_events(tmp_path / "events.ndjson", without("EngineFinished")), 200)
    assert not report.complete and len(report.failures) == 11


def test_unknown_events_and_broken_lines_are_counted(tmp_path):
    path = write_events(tmp_path / "events.ndjson", [{"Mystery": {}}, *events()])
    path.write_text(path.read_text(encoding="utf-8") + '{"ScenarioFinished": {"recorder"\n', encoding="utf-8")
    report = report_parser.parse(path, 200)
    assert report.complete and report.unknown == 2


def test_non_fatal_errors_and_missing_file(tmp_path):
    error = {"NonFatalError": {"label": "GET /api/Order/{id}", "value": {"type": "InvalidSchema", "message": "bad"}}}
    report = report_parser.parse(write_events(tmp_path / "events.ndjson", [error, *events()]), 200)
    assert report.errors == ("GET /api/Order/{id}：InvalidSchema：bad",)
    assert report_parser.parse(tmp_path / "none.ndjson", 200) == report_parser.RoleReport(False)
