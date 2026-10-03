import json

import pytest
from api_fuzz_reports import stub_model
from probe_world import NOW, RELEASE, make_redactor, make_target

from tightrein.domain.enums import Probe, Source
from tightrein.domain.signal import Signal
from tightrein.sources.api_fuzz.replay import RecordedRequest, replay, send, signal_replayer
from tightrein.sources.common.http import HttpResponse
from tightrein.sources.common.raw import RawDir
from tightrein.sources.common.session import ANONYMOUS_ROLE, LoginSettings, Session

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiQ29tcGFueSJ9.bmV3LXRva2Vu"


class Credentials:
    def account(self, role):
        return f"test-{role.lower()}"

    def password(self, role):
        return "pw"


class Transport:
    """登录请求返回 token，其他请求按 answer 应答。"""

    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.endswith("/api/Auth/Login"):
            return HttpResponse(200, json.dumps({"data": {"token": TOKEN}}).encode())
        return self.answer


def run(tmp_path, answer, check, request=None, **extra):
    transport = Transport(answer)
    redactor = make_redactor()
    session = Session("https://staging.example.test", LoginSettings("/api/Auth/Login", {}, "data.token"),
                      Credentials(), transport, redactor)
    recorded = request or RecordedRequest("POST", "/api/Order/Query", "/api/Order/Query", {"company": "a b"},
                                          {"pageSize": 0})
    result = replay(recorded, "Company", make_target(tmp_path), session, transport, check=check, redactor=redactor,
                    timeout_seconds=30, **extra)
    return result, transport.requests[-1]


def test_replay_sends_the_recorded_request_with_a_fresh_token(tmp_path):
    result, request = run(tmp_path, HttpResponse(500, b'{"error": "boom"}', elapsed_ms=40), "not_a_server_error")
    assert (request.method, request.url) == ("POST", "https://staging.example.test/api/Order/Query?company=a+b")
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.body) == {"pageSize": 0}
    assert (result.status, result.elapsed_ms, result.still_failing) == (500, 40, True)
    assert result.excerpt == '{"error": "boom"}'


def test_replay_after_the_fix(tmp_path):
    result, _ = run(tmp_path, HttpResponse(400, b"{}"), "not_a_server_error")
    assert result.still_failing is False


def test_unauthorized_replay_uses_the_model(tmp_path):
    request = RecordedRequest("GET", "/api/User/List", "/api/User/List")
    result, sent = run(tmp_path, HttpResponse(200, b"[]"), "unauthorized_role_access", request, model=stub_model())
    assert result.still_failing is True and sent.body is None and "Content-Type" not in sent.headers
    denied, _ = run(tmp_path, HttpResponse(403, b"{}"), "unauthorized_role_access", request, model=stub_model())
    assert denied.still_failing is False


def test_unanswered_and_undecidable(tmp_path):
    result, _ = run(tmp_path, HttpResponse(None, error="URLError: timed out"), "not_a_server_error")
    assert (result.status, result.still_failing, result.error) == (None, None, "URLError: timed out")
    schema, _ = run(tmp_path, HttpResponse(200, b"[]"), "response_schema_conformance")
    assert schema.still_failing is None


def test_request_from_signal_context_reads_the_body_reference(tmp_path):
    raw = RawDir(tmp_path / "raw")
    raw.write_json("refs/Company-c1-body.json", {"rows": [1, 2]})
    context = {"method": "post", "path": "/api/Order/Query", "pathTemplate": "/api/Order/Query", "query": {},
               "bodyRef": "refs/Company-c1-body.json"}
    assert RecordedRequest.from_context(context, raw) == RecordedRequest(
        "POST", "/api/Order/Query", "/api/Order/Query", {}, {"rows": [1, 2]})
    with pytest.raises(ValueError, match="原始输出目录"):
        RecordedRequest.from_context(context)


def make_signal(context, actor):
    return Signal(id="S-01J9Z3" + "0" * 20, run_id="R-20261005-030000-collect-api-fuzz", source=Source.SYNTHETIC,
                  probe=Probe.API_FUZZ, check="not_a_server_error", environment="staging", occurred_at=NOW,
                  release=RELEASE, location="GET /api/Order/42", message="500", context=context, actor=actor)


def replayer_for(tmp_path, answer):
    transport = Transport(answer)
    redactor = make_redactor()
    session = Session("https://staging.example.test", LoginSettings("/api/Auth/Login", {}, "data.token"),
                      Credentials(), transport, redactor)
    return signal_replayer(make_target(tmp_path), session, transport, redactor, lambda signal: RawDir(tmp_path),
                           30), \
        transport


def test_signal_replayer_replays_each_attempt(tmp_path):
    replayer, transport = replayer_for(tmp_path, HttpResponse(500, b"{}"))
    context = {"request": {"method": "GET", "path": "/api/Order/42", "pathTemplate": "/api/Order/{id}"}}
    signal = make_signal(context, {"role": "Company"})
    assert replayer(signal, 2) == [True, True]
    assert [request.url for request in transport.requests].count("https://staging.example.test/api/Order/42") == 2


def test_signal_replayer_without_a_request_cannot_judge(tmp_path):
    replayer, transport = replayer_for(tmp_path, HttpResponse(500, b"{}"))
    assert replayer(make_signal({}, {"role": "Company"}), 2) == [None, None]
    assert transport.requests == []


def test_send_uses_the_static_header_or_no_credentials(tmp_path):
    transport = Transport(HttpResponse(200, b"{}"))
    settings = LoginSettings(None, None, None, "static-header", "X-Access-Code")
    session = Session("https://staging.example.test", settings, Credentials(), transport, make_redactor())
    recorded = RecordedRequest("GET", "/api/answers", "/api/answers")
    send(recorded, "Learner", make_target(tmp_path), session, transport, 30)
    send(recorded, ANONYMOUS_ROLE, make_target(tmp_path), session, transport, 30)
    static, anonymous = transport.requests
    assert static.headers["X-Access-Code"] == "pw" and "Authorization" not in static.headers
    assert anonymous.headers == {"Accept": "application/json"}
