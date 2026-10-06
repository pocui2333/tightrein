import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from api_fuzz_reports import RECORDED_TOKEN, stub_model, write_events
from api_fuzz_world import SPEC
from probe_world import NOW, RELEASE, CountingRandom, make_redactor, make_target

from tightrein.domain.enums import Probe, Source
from tightrein.domain.reproduce import API_FUZZ_REPLAY_CHECKS
from tightrein.sources.api_fuzz import hooks, mapping, report_parser
from tightrein.sources.api_fuzz.report_parser import Failure, RecordedCase, RecordedInteraction
from tightrein.sources.common.signals import SignalFactory

CONTEXT = mapping.RoleContext("Company", "Company", NOW, 20261005, RELEASE, (RECORDED_TOKEN,))


def run_mapping(tmp_path, model):
    report = report_parser.parse(write_events(tmp_path / "events.ndjson"), 200)
    redactor = make_redactor(RECORDED_TOKEN)
    factory = SignalFactory(make_target(tmp_path), Probe.API_FUZZ, redactor, randomness=CountingRandom())
    return mapping.to_signals(report, CONTEXT, model, factory, redactor, "https://staging.example.test")


def by_check(result):
    return {signal.check: signal for signal in result.signals}


def test_expected_denials_are_dropped_with_a_model(tmp_path):
    result = run_mapping(tmp_path, stub_model())
    assert (result.expected_denials, result.unjudged_auth_failures) == (2, 0)
    assert sorted((signal.check, signal.location, signal.context["sameCaseCount"]) for signal in result.signals) == [
        ("max_response_time", "GET /api/Report/Export", 2),
        ("not_a_server_error", "POST /api/Order/Query", 1),
        ("response_schema_conformance", "GET /api/Order/List", 2),
        ("status_code_conformance", "POST /api/Order/Query", 1),
        ("unauthorized_role_access", "GET /api/User/List", 2),
        ("unsupported_method", "POST /api/Order/Query", 1),
    ]


def test_auth_failures_are_unjudged_without_a_model(tmp_path):
    result = run_mapping(tmp_path, None)
    assert (result.expected_denials, result.unjudged_auth_failures) == (0, 2)
    assert "status_code_conformance" in by_check(result)


def test_server_error_signal(tmp_path):
    signal = by_check(run_mapping(tmp_path, stub_model()))["not_a_server_error"]
    assert (signal.source, signal.probe, signal.release, signal.message) == (
        Source.SYNTHETIC, Probe.API_FUZZ, RELEASE, "服务端返回 500")
    assert signal.occurred_at == datetime.fromtimestamp(1790721552, timezone.utc)
    assert signal.actor == {"id": "Company", "role": "Company"}
    context = signal.context
    assert context["request"] == {"method": "POST", "path": "/api/Order/Query", "pathTemplate": "/api/Order/Query",
                                  "query": {}, "body": {"pageSize": 0}}
    assert context["response"]["status"] == 500 and "negative or zero" in context["response"]["bodyExcerpt"]
    assert context["reproduce"] == ("curl -X POST 'http://127.0.0.1:18282/api/Order/Query' "
                                    "-H 'Authorization: Bearer <TOKEN>' -H 'Content-Type: application/json' "
                                    "-d '{\"pageSize\": 0}'")
    assert (context["seed"], context["reportPath"], context["phase"]) == (20261005, "Company", "fuzzing")
    assert RECORDED_TOKEN not in json.dumps(signal.context)


def test_messages_and_capabilities(tmp_path):
    signals = by_check(run_mapping(tmp_path, stub_model()))
    assert signals["response_schema_conformance"].message == 'Response violates schema：1 is not of type "string"'
    assert signals["max_response_time"].message == "Response time limit exceeded：Actual: 1211.37ms"
    denied = signals["unauthorized_role_access"]
    assert denied.message == "角色 Company 缺少能力 CanManageUsers，GET /api/User/List 却返回 200"
    assert denied.context["requiredCapabilities"] == ["CanManageUsers"]
    assert denied.context["grantedCapabilities"] == ["CanViewOrders"]
    assert "requiredCapabilities" not in signals["not_a_server_error"].context


def failure(check="not_a_server_error", body=None, interaction=True, status=500):
    recorded = RecordedInteraction("POST", "https://h/api/Order/Query?token=abc", status, 12, b"{}", None)
    return Failure(("POST", "/api/Order/Query"), check, "Server error", "", "fuzzing", "c1",
                   RecordedCase("POST", "/api/Order/Query", {"token": "abc"}, body), recorded if interaction else None)


def test_large_bodies_move_to_raw_and_fallbacks(tmp_path):
    redactor = make_redactor()
    factory = SignalFactory(make_target(tmp_path), Probe.API_FUZZ, redactor, randomness=CountingRandom())
    body = {"rows": ["x" * 100] * 200}
    report = report_parser.RoleReport(True, failures=(failure(body=body), failure("custom", interaction=False)))
    result = mapping.to_signals(report, CONTEXT, None, factory, redactor, "https://staging.example.test/")
    first, second = result.signals
    assert first.context["request"]["bodyRef"] == "refs/Company-c1-body.json"
    assert json.loads((tmp_path / "raw" / "api-fuzz" / "refs" / "Company-c1-body.json").read_text()) == body
    assert first.context["request"]["query"] == {"token": "[已脱敏]"}
    assert "token=[已脱敏]" in first.context["reproduce"]
    assert first.context["reproduce"].endswith("-d '@refs/Company-c1-body.json'")
    assert second.occurred_at == NOW and second.context["response"] is None
    assert second.context["reproduce"].startswith("curl -X POST 'https://staging.example.test/api/Order/Query'")


@pytest.mark.parametrize(("check", "status", "elapsed", "extra", "expected"), [
    ("not_a_server_error", 502, 10, {}, True),
    ("not_a_server_error", 200, 10, {}, False),
    ("unauthorized_role_access", 200, 10, {"missing_capabilities": ("CanManageUsers",)}, True),
    ("unauthorized_role_access", 403, 10, {"missing_capabilities": ("CanManageUsers",)}, False),
    ("unauthorized_role_access", 200, 10, {}, None),
    ("max_response_time", 200, 1500, {"max_response_ms": 1000}, True),
    ("max_response_time", 200, 900, {"max_response_ms": 1000}, False),
    ("status_code_conformance", 418, 10, {"documented_statuses": ("200", "400")}, True),
    ("status_code_conformance", 400, 10, {"documented_statuses": ("200", "400")}, False),
    ("status_code_conformance", 418, 10, {"documented_statuses": ("default",)}, False),
    ("response_schema_conformance", 200, 10, {}, None),
    ("not_a_server_error", None, 10, {}, None),
])
def test_judge(check, status, elapsed, extra, expected):
    assert mapping.judge(check, status, elapsed, **extra) is expected


def test_documented_statuses():
    assert mapping.documented_statuses(SPEC, "GET", "/api/Order/{id}") == ("200",)
    assert mapping.documented_statuses(SPEC, "DELETE", "/api/Order/{id}") is None


def test_hook_violation_rules():
    model = stub_model()
    assert hooks.violation(model, "Company", "GET", "/api/User/List", 200) == (
        "角色 Company 缺少能力 CanManageUsers，GET /api/User/List 却返回 200")
    assert hooks.violation(model, "Company", "GET", "/api/User/List", 403) is None
    assert hooks.violation(model, "Company", "POST", "/api/Auth/Login", 200) is None
    assert hooks.violation(model, "Company", "GET", "/api/Order/{id}", 200) is None
    assert hooks.violation(model, "Company", "GET", "/api/Unknown", 200) is None


def test_hook_check_raises_assertion_error():
    check = hooks.make_check(stub_model(), "Company")
    assert check.__name__ == "unauthorized_role_access"
    case = SimpleNamespace(method="GET", path="/api/User/List")
    with pytest.raises(AssertionError, match="CanManageUsers"):
        check(None, SimpleNamespace(status_code=200), case)
    check(None, SimpleNamespace(status_code=403), case)


def test_hook_registration_needs_both_variables(tmp_path, monkeypatch):
    registered = []
    monkeypatch.setattr(hooks.schemathesis, "check", registered.append)
    path = tmp_path / "authz-model.json"
    path.write_text(json.dumps(stub_model().to_document("2026-10-05T03:00:00Z")), encoding="utf-8")
    assert hooks.register({hooks.MODEL_ENV: str(path)}) is False
    assert hooks.register({hooks.ROLE_ENV: "Company"}) is False
    assert hooks.register({hooks.MODEL_ENV: str(path), hooks.ROLE_ENV: "Company"}) is True
    assert [item.__name__ for item in registered] == ["unauthorized_role_access"]


def test_reproduce_writes_the_credential_header_of_the_login_kind():
    static = mapping.reproduce("GET", "https://h/api/answers", None, None, auth=("X-Access-Code", ""))
    assert static == "curl -X GET 'https://h/api/answers' -H 'X-Access-Code: <TOKEN>'"
    assert mapping.reproduce("GET", "https://h/api/questions", None, None, auth=None) == (
        "curl -X GET 'https://h/api/questions'")
    assert make_redactor().reproduce(static, ("code-7f3a9c41",)) == static


@pytest.mark.parametrize("check", sorted(API_FUZZ_REPLAY_CHECKS))
def test_replay_checks_are_judged_from_the_status_alone(check):
    """复现确认按重放处理的检查，重放器不带权限模型、响应时间阈值与接口描述也能判定。"""
    assert mapping.judge(check, 500, 10) is True
    assert mapping.judge(check, 200, 10) is False


@pytest.mark.parametrize("check", ["unsupported_method", mapping.STATUS_CHECK, mapping.UNAUTHORIZED_CHECK])
def test_other_checks_cannot_be_judged_by_the_replayer(check):
    assert mapping.judge(check, 200, 10) is None
