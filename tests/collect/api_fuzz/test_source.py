import json
import tomllib
from pathlib import Path

import pytest

from tightrein.collect.api_fuzz import source
from tightrein.collect.common.signals import DEPLOYMENTS_KEY
from tightrein.collect.common.source import SourceInvalid, SourceMisconfigured, SourceStatus, SourceUnavailable
from tightrein.protocol.http import HttpResponse
from tightrein.protocol.process import Outcome
from tightrein.protocol.raw import raw_dir
from tightrein.store.tables import state

SPEC = json.dumps({"openapi": "3.0.1", "paths": {
    "/api/Order/Query": {"post": {}}, "/api/Order/List": {"get": {}}, "/api/Order/{id}": {"get": {}}}})
SITES = {"baseUrl": "http://127.0.0.1:18282", "environment": "staging", "health": "/healthz",
         "login": {"endpoint": "/api/Auth/Login", "tokenPath": "data.token",
                   "bodyTemplate": {"u": "{account}", "p": "{password}"}}}
SECRETS = {"api_fuzz.account": "test-company", "api_fuzz.password": "pw-1"}
DEPLOY = "d6f37025a1b2c3d4e5f60718293a4b5c6d7e8f90"


class FakeGit:
    main_branch = "main"

    def show(self, rev, path):
        return SPEC if path == "openapi.json" else None


HEALTHY = HttpResponse(200, b"ok")


class Transport:
    def __init__(self, token, health=HEALTHY):
        self.token = token
        self.health = health
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.endswith("/healthz"):
            return self.health
        return HttpResponse(200, json.dumps({"data": {"token": self.token}}).encode())


class SchemathesisRunner:
    """把录制的事件流写进报告目录，代替真正的 Schemathesis。"""

    def __init__(self, write_events, exit_code=1, drop=None):
        self.write_events = write_events
        self.exit_code = exit_code
        self.drop = drop
        self.commands = []

    def run(self, command):
        self.commands.append(command)
        self.write_events(Path(command.cwd) / "events.ndjson", drop=self.drop)
        return Outcome(self.exit_code, "", "", 10, None, None)


def make(source_runtime, *, sites=SITES, secrets=SECRETS, runner=None, run="R-20261005T030000Z-collect"):
    runtime = source_runtime(modules={"collect.api_fuzz": {"method": "openapi_file"}},
                             controls={"collect.api_fuzz": {"openapi_file": {"path": "openapi.json", "base": "repo"}}},
                             sites=split_sites(sites), secrets=secrets, runner=runner)
    runtime.git = FakeGit()
    runtime.run = run
    # 部署早于录制的交互时间(2026-09-29)：信号的 commit 是交互发生时在线的版本
    state.put(runtime.conn, DEPLOYMENTS_KEY, [{"commit": DEPLOY, "status": "succeeded",
                                               "deployedAt": "2026-09-28T00:00:00Z",
                                               "detectedAt": "2026-09-28T00:05:00Z"}], runtime.clock)
    return runtime


def split_sites(sites):
    """被测地址与环境在 sites.json 的 target(各来源共用)，其余在 api_fuzz。"""
    shared = ("baseUrl", "environment")
    return {"target": {key: sites[key] for key in shared if key in sites},
            "api_fuzz": {key: value for key, value in sites.items() if key not in shared}}


def collect(runtime, transport):
    return source.collect(runtime, transport=transport, seed_source=lambda: 42, installed_problem=lambda: None,
                          command=Path("/venv/bin/schemathesis"))


def test_without_a_target_it_is_skipped(source_runtime):
    result = collect(make(source_runtime, sites={}), Transport("t"))
    assert result.status is SourceStatus.SKIPPED and "baseUrl" in result.reason


def test_production_misconfiguration_stops_before_any_request(source_runtime, write_events):
    runner = SchemathesisRunner(write_events)
    transport = Transport("t")
    with pytest.raises(SourceMisconfigured, match="sites.api_fuzz.allow"):
        collect(make(source_runtime, sites={**SITES, "environment": "production"}, runner=runner), transport)
    assert runner.commands == [] and transport.requests == []


def test_a_wrong_schemathesis_version_fails(source_runtime):
    with pytest.raises(SourceUnavailable, match="版本"):
        source.collect(make(source_runtime), transport=Transport("t"), installed_problem=lambda: "版本为 4.1.0")


def test_a_failed_health_check_skips_without_calling_schemathesis(source_runtime, write_events):
    runner = SchemathesisRunner(write_events)
    result = collect(make(source_runtime, runner=runner), Transport("t", HttpResponse(503)))
    assert result.status is SourceStatus.SKIPPED and "健康检查未通过" in result.reason
    assert runner.commands == [] and result.state == {}


def test_a_full_run(source_runtime, write_events, recorded_token):
    runner = SchemathesisRunner(write_events)
    runtime = make(source_runtime, runner=runner)
    result = collect(runtime, Transport(recorded_token))
    assert result.status is SourceStatus.DONE
    assert [(signal.check_type, signal.location) for signal in result.signals] == [
        ("server_error", "POST /api/Order/Query")]
    assert result.signals[0].commit == DEPLOY and result.signals[0].environment == "staging"
    assert "POST /api/Order/Query" in result.coverage and len(result.coverage) == 6
    assert result.state[source.STATE_KEY]["deploy"] == DEPLOY
    command = runner.commands[0]
    assert recorded_token not in " ".join(command.argv) and command.env["TIGHTREIN_TOKEN"] == recorded_token
    raw = raw_dir(runtime.workspace, runtime.run, source.SOURCE)
    config = tomllib.loads((raw / "schemathesis.toml").read_text(encoding="utf-8"))
    assert config["headers"] == {"Authorization": "Bearer ${TIGHTREIN_TOKEN}"}
    # 报告目录中的 token 按字节替换掉
    for path in raw.rglob("*"):
        if path.is_file():
            assert recorded_token.encode() not in path.read_bytes()
    assert recorded_token not in json.dumps([signal.to_json() for signal in result.signals])


def test_the_same_deployment_and_description_are_tested_once(source_runtime, write_events, recorded_token):
    runner = SchemathesisRunner(write_events)
    runtime = make(source_runtime, runner=runner)
    first = collect(runtime, Transport(recorded_token))
    for key, value in first.state.items():
        state.put(runtime.conn, key, value, runtime.clock)
    second = collect(runtime, Transport(recorded_token))
    assert second.status is SourceStatus.SKIPPED and second.reason == source.UNCHANGED
    assert len(runner.commands) == 1


def test_an_anonymous_run_has_no_credentials(source_runtime, write_events):
    runner = SchemathesisRunner(write_events)
    sites = {key: value for key, value in SITES.items() if key not in ("login", "health")}
    runtime = make(source_runtime, sites=sites, secrets={}, runner=runner, run="R-anonymous")
    result = collect(runtime, Transport("unused"))
    assert result.status is SourceStatus.DONE and source.NO_HEALTH in result.notes
    assert "TIGHTREIN_TOKEN" not in runner.commands[0].env
    raw = raw_dir(runtime.workspace, runtime.run, source.SOURCE)
    assert "headers" not in tomllib.loads((raw / "schemathesis.toml").read_text(encoding="utf-8"))
    assert "Authorization" not in result.signals[0].evidence["reproduce"]


def test_failed_calls_and_incomplete_reports_produce_nothing(source_runtime, write_events, recorded_token):
    with pytest.raises(SourceUnavailable, match="退出码 2"):
        collect(make(source_runtime, runner=SchemathesisRunner(write_events, exit_code=2)), Transport(recorded_token))
    with pytest.raises(SourceInvalid, match="不完整"):
        collect(make(source_runtime, runner=SchemathesisRunner(write_events, drop="EngineFinished"), run="R-x"),
                Transport(recorded_token))
