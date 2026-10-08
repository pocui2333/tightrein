import json
from datetime import UTC, datetime

from tightrein.collect.api_fuzz import mapping
from tightrein.collect.api_fuzz.schemathesis import report_parser
from tightrein.collect.api_fuzz.schemathesis.report_parser import Failure, RecordedCase, RecordedInteraction, Report
from tightrein.collect.common.signals import SignalFactory, SignalLimits
from tightrein.protocol.naming import FixedClock
from tightrein.protocol.raw import RawDir
from tightrein.protocol.security import Redactor

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
RUN = "R-20261005T030000Z-collect"


def factory(tmp_path, redactor):
    return SignalFactory(run=RUN, source="collect.api_fuzz", clock=FixedClock(NOW), redactor=redactor,
                         raw=RawDir(tmp_path / "raw"), limits=SignalLimits(1000, 16384, 500))


def context(auth=("Authorization", "Bearer "), body_bytes=16384):
    return mapping.MappingContext(run=RUN, finished_at=NOW, seed=20261005, base_url="https://staging.example.test",
                                  auth=auth, environment="staging", report_path="report", body_bytes=body_bytes,
                                  release_at=lambda at: "c0ffee")


def test_only_server_errors_become_signals_one_per_operation(tmp_path, write_events, recorded_token):
    redactor = Redactor()
    redactor.register(recorded_token)
    report = report_parser.parse(write_events(tmp_path / "events.ndjson"), 200)
    signals = mapping.to_signals(report, context(), factory(tmp_path, redactor), redactor)
    assert len(signals) == 1
    signal = signals[0]
    assert (signal.check_type, signal.location, signal.message) == ("server_error", "POST /api/Order/Query",
                                                                    "服务端返回 500")
    assert signal.deterministic and not signal.verified and signal.commit == "c0ffee"
    assert signal.occurred_at == "2026-09-29T22:39:12Z"
    evidence = signal.evidence
    assert evidence["request"] == {"method": "POST", "path": "/api/Order/Query", "pathTemplate": "/api/Order/Query",
                                   "query": {}, "body": {"pageSize": 0}}
    assert evidence["status"] == 500 and evidence["response"]["status"] == 500
    assert "negative or zero" in evidence["response"]["bodyExcerpt"]
    assert (evidence["seed"], evidence["sameCaseCount"], evidence["run"], evidence["phase"]) == (
        20261005, 1, RUN, "fuzzing")
    assert evidence["reproduce"].startswith("curl -X POST 'http://127.0.0.1:18282/api/Order/Query'")
    assert "<TOKEN>" in evidence["reproduce"]
    assert recorded_token not in json.dumps(evidence)


def failure(case_id="c1", body=None, interaction=True, status=500):
    recorded = RecordedInteraction("POST", "https://h/api/Order/Query?token=abc", status, 12, b"{}", None)
    return Failure(("POST", "/api/Order/Query"), "not_a_server_error", "Server error", "", "fuzzing", case_id,
                   RecordedCase("POST", "/api/Order/Query", {"token": "abc"}, body), recorded if interaction else None)


def test_same_operation_failures_are_counted_and_large_bodies_move_to_refs(tmp_path):
    redactor = Redactor()
    body = {"rows": ["x" * 100] * 50}
    report = Report(True, failures=(failure(body=body), failure("c2"), failure("c3")))
    signals = mapping.to_signals(report, context(body_bytes=1000), factory(tmp_path, redactor), redactor)
    assert len(signals) == 1 and signals[0].evidence["sameCaseCount"] == 3
    request = signals[0].evidence["request"]
    assert request["bodyRef"] == "refs/c1-body.json" and "body" not in request
    assert json.loads((tmp_path / "raw" / "refs" / "c1-body.json").read_text(encoding="utf-8")) == body
    assert request["query"]["token"] != "abc"
    assert signals[0].evidence["reproduce"].endswith("-d '@refs/c1-body.json'")


def test_url_query_parameters_are_redacted_by_key_and_the_fragment_is_dropped(tmp_path):
    redactor = Redactor()
    recorded = RecordedInteraction("GET", "https://h/api/items?page=2&access_token=abc&q=#state=xyz", 500, 12, b"{}",
                                   None)
    found = Failure(("GET", "/api/items"), "not_a_server_error", "Server error", "", "fuzzing", "c1",
                    RecordedCase("GET", "/api/items", {"page": "2"}, None), recorded)
    signals = mapping.to_signals(Report(True, failures=(found,)), context(auth=None), factory(tmp_path, redactor),
                                 redactor)
    command = signals[0].evidence["reproduce"]
    assert command == "curl -X GET 'https://h/api/items?page=2&access_token=[REDACTED:credential]&q='"
    assert "abc" not in command and "xyz" not in command and "#" not in command


def test_without_an_interaction_the_call_end_and_the_base_url_are_used(tmp_path):
    redactor = Redactor()
    signals = mapping.to_signals(Report(True, failures=(failure(interaction=False),)), context(auth=None),
                                 factory(tmp_path, redactor), redactor)
    evidence = signals[0].evidence
    assert signals[0].occurred_at == "2026-10-05T03:00:00Z" and signals[0].message == "Server error"
    assert evidence["response"] is None and evidence["status"] is None
    assert evidence["reproduce"] == "curl -X POST 'https://staging.example.test/api/Order/Query'"


def test_reproduce_writes_the_credential_header_of_the_login_kind():
    static = mapping.reproduce("GET", "https://h/api/answers", None, None, None, ("X-Access-Code", ""))
    assert static == "curl -X GET 'https://h/api/answers' -H 'X-Access-Code: <TOKEN>'"
    assert mapping.reproduce("GET", "https://h/api/questions", None, None, None, None) == (
        "curl -X GET 'https://h/api/questions'")
    bearer = mapping.reproduce("GET", "https://h/a", None, None, None, ("Authorization", "Bearer "))
    # 文本脱敏会把 Authorization 请求头整个换掉，脱敏后改回占位符
    assert mapping.redact_reproduce(bearer, Redactor(), ("Authorization", "Bearer ")) == (
        "curl -X GET 'https://h/a' -H 'Authorization: Bearer <TOKEN>'")
