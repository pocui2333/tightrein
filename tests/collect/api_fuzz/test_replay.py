import json

from tightrein.collect.api_fuzz import replay
from tightrein.collect.api_fuzz.replay import RecordedRequest
from tightrein.collect.dedup import reproduce
from tightrein.protocol.http import HttpResponse
from tightrein.protocol.raw import RawDir, raw_dir

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiQ29tcGFueSJ9.bmV3LXRva2Vu"
SITES = {"target": {"baseUrl": "https://staging.example.test"}, "api_fuzz": {"login": {
    "endpoint": "/api/Auth/Login", "tokenPath": "data.token", "bodyTemplate": {"u": "{account}", "p": "{password}"}}}}
SECRETS = {"api_fuzz.account": "test-company", "api_fuzz.password": "pw-1"}
EVIDENCE = {"run": "R-20261004T030000Z-collect",
            "request": {"method": "post", "path": "/api/Order/Query", "pathTemplate": "/api/Order/Query",
                        "query": {"company": "a b"}, "body": {"pageSize": 0}}}


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


def _with_run(source_runtime, run):
    made = source_runtime(sites=SITES, secrets=SECRETS)
    made.run = run
    return made


def test_replay_sends_the_recorded_request_with_a_fresh_token(source_runtime):
    runtime = _with_run(source_runtime, "R-replay-1")
    transport = Transport(HttpResponse(500, b'{"error": "boom"}'))
    assert replay.replay(runtime, EVIDENCE, transport=transport) == "reproduced"
    sent = transport.requests[-1]
    assert (sent.method, sent.url) == ("POST", "https://staging.example.test/api/Order/Query?company=a+b")
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}" and sent.headers["Content-Type"] == "application/json"
    assert json.loads(sent.body) == {"pageSize": 0}
    # 同一次运行只登录一次
    assert replay.replay(runtime, EVIDENCE, transport=transport) == "reproduced"
    assert [request.url for request in transport.requests].count("https://staging.example.test/api/Auth/Login") == 1


def test_only_the_status_decides(source_runtime):
    runtime = _with_run(source_runtime, "R-replay-2")
    assert replay.replay(runtime, EVIDENCE, transport=Transport(HttpResponse(400, b"{}"))) == "not_reproduced"
    assert replay.replay(runtime, EVIDENCE,
                         transport=Transport(HttpResponse(None, error="URLError: timed out"))) == "unavailable"


def test_without_a_request_a_target_or_a_login_it_cannot_judge(source_runtime):
    runtime = _with_run(source_runtime, "R-replay-3")
    transport = Transport(HttpResponse(500, b"{}"))
    assert replay.replay(runtime, {"run": EVIDENCE["run"]}, transport=transport) == "unavailable"
    assert transport.requests == []
    no_target = source_runtime(sites={}, secrets=SECRETS)
    assert replay.replay(no_target, EVIDENCE, transport=transport) == "unavailable"
    no_secrets = source_runtime(sites=SITES, secrets={})
    no_secrets.run = "R-replay-4"
    assert replay.replay(no_secrets, EVIDENCE, transport=transport) == "unavailable"


def test_the_body_reference_is_read_from_the_raw_directory_of_the_signal_run(source_runtime):
    runtime = _with_run(source_runtime, "R-replay-5")
    raw = RawDir(raw_dir(runtime.workspace, EVIDENCE["run"], "collect.api_fuzz"))
    raw.write_json("refs/c1-body.json", {"rows": [1, 2]})
    evidence = {**EVIDENCE, "request": {**EVIDENCE["request"], "bodyRef": "refs/c1-body.json"}}
    assert RecordedRequest.from_evidence(evidence, raw) == RecordedRequest(
        "POST", "/api/Order/Query", {"company": "a b"}, {"rows": [1, 2]})
    moved = {"run": EVIDENCE["run"], "requestRef": raw.write_json("refs/S-1-request.json", evidence["request"])}
    assert RecordedRequest.from_evidence(moved, raw).body == {"rows": [1, 2]}
    transport = Transport(HttpResponse(502, b"{}"))
    assert replay.replay(runtime, evidence, transport=transport) == "reproduced"
    assert json.loads(transport.requests[-1].body) == {"rows": [1, 2]}


def test_a_target_with_a_path_prefix_is_not_repeated():
    assert replay.relative_path("https://h/api/", "/api/Order/Query") == "/Order/Query"
    assert replay.relative_path("https://h", "/api/Order/Query") == "/api/Order/Query"
    assert replay.relative_path("https://h/api", "/apix/Order") == "/apix/Order"


def test_dedup_finds_the_replayer_and_shares_its_three_verdicts(source_runtime):
    runtime = _with_run(source_runtime, "R-replay-6")
    transport = Transport(HttpResponse(503, b"{}"))
    found = reproduce.replayer_for("collect.api_fuzz",
                                   lambda function: lambda evidence: function(runtime, evidence, transport=transport))
    assert found is not None and found(EVIDENCE) == reproduce.REPRODUCED
    assert (replay.REPRODUCED, replay.NOT_REPRODUCED, replay.UNAVAILABLE) == (
        reproduce.REPRODUCED, reproduce.NOT_REPRODUCED, reproduce.UNAVAILABLE)
