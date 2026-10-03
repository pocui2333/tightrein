import json
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from api_fuzz_reports import RECORDED_TOKEN, stub_model, without, write_events
from api_fuzz_world import ENDPOINTS, ROLES, FakeClient, default, error, ok, spec_result, write_spec
from probe_world import RELEASE, CountingRandom, make_redactor, make_target

from tightrein.domain.enums import ExtensionLayer, ExtensionPoint, ProbeLevel, RunStatus
from tightrein.sources.api_fuzz import hooks
from tightrein.sources.api_fuzz.authz import model as authz_model
from tightrein.sources.api_fuzz.probe import (ANONYMOUS_CONFIG_FILE, ANONYMOUS_ONLY, ApiFuzzDependencies, ApiFuzzProbe,
                                             scrub_tokens)
from tightrein.sources.base import ProbeOptions
from tightrein.sources.common.http import HttpResponse
from tightrein.sources.common.procs import ToolRun
from tightrein.sources.common.session import ANONYMOUS_ROLE, NO_TARGET, LoginSettings, Session
from tightrein.store.files.layout import WorkspaceLayout

ROLES_CONFIG = {"Company": {"keychain": "tightrein.demo.company"}, "Admin": {"keychain": "tightrein.demo.admin"}}


class Credentials:
    def account(self, role):
        return role

    def password(self, role):
        return "pw"


class LoginTransport:
    def __init__(self, failing=()):
        self.failing = set(failing)

    def __call__(self, request):
        role = json.loads(request.body)["userName"]
        if role in self.failing:
            return HttpResponse(401)
        return HttpResponse(200, json.dumps({"data": {"token": RECORDED_TOKEN}}).encode())


class FakeSchemathesis:
    """按角色写出录制的事件流(其中的用例请求头带着登录得到的 token)，返回给定的退出码。"""

    def __init__(self, events_by_role=None, runs=None):
        self.events_by_role = events_by_role or {}
        self.runs = runs or {}
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        role_dir = Path(command.cwd)
        role = role_dir.name
        if role in self.events_by_role:
            write_events(role_dir / "events.ndjson", self.events_by_role[role])
        return self.runs.get(role, ToolRun(1))


class World:
    def __init__(self, tmp_path, make_config, *, client=None, failing=(), launcher=None, installed=None):
        self.tmp_path = tmp_path
        self.layout = WorkspaceLayout(tmp_path / "workspace")
        spec_path = write_spec(self.layout.spec_dir(RELEASE))
        self.client = client or FakeClient(
            spec_export=spec_result(spec_path), authz_endpoints=ok(ExtensionPoint.AUTHZ_ENDPOINTS, ENDPOINTS),
            authz_roles=ok(ExtensionPoint.AUTHZ_ROLES, ROLES, layer=ExtensionLayer.PROJECT))
        self.redactor = make_redactor()
        self.config = make_config(accounts={"roles": ROLES_CONFIG, "login": {
            "endpoint": "/api/Auth/Login", "bodyTemplate": {"userName": "{account}", "password": "{password}"},
            "tokenPath": "data.token"}})
        self.session = Session("https://staging.example.test", LoginSettings.from_config(self.config), Credentials(),
                               LoginTransport(failing), self.redactor)
        self.launcher = launcher or FakeSchemathesis({"Company": None, "Admin": None})
        self.probe = ApiFuzzProbe(ApiFuzzDependencies(
            self.config, self.client, self.session, self.launcher, self.layout, self.redactor,
            environ={"PATH": "/bin"}, randomness=CountingRandom(), seed_source=lambda: 7,
            installed_problem=installed or (lambda: None), command=Path("/venv/bin/schemathesis")))
        self.target = make_target(tmp_path, worktree=tmp_path / "worktree")

    def run(self, level=ProbeLevel.SHALLOW, **options):
        return self.probe.run(self.target, level, ProbeOptions(**options))


def test_full_run(tmp_path, make_config):
    world = World(tmp_path, make_config)
    outcome = world.run()
    assert outcome.status is RunStatus.OK
    assert len(outcome.signals) == 12
    assert {signal.actor["role"] for signal in outcome.signals} == {"Company", "Admin"}
    assert outcome.coverage.methods == "GET" and outcome.coverage.endpoints_total == 6
    assert len(outcome.coverage.endpoints) == 12
    assert outcome.stats["rolesTested"] == 2 and outcome.stats["operationsTested"] == 6
    assert outcome.stats["seed"] == 7 and outcome.stats["signals"] == 12
    assert outcome.environment.failed_roles == () and outcome.environment.report_complete
    assert "schemathesis.toml" in outcome.artifacts and "Company/events.ndjson" in outcome.artifacts
    assert set(outcome.extensions) == {"spec-export", "authz-endpoints", "authz-roles"}


def test_tokens_stay_out_of_command_lines_raw_files_and_signals(tmp_path, make_config):
    world = World(tmp_path, make_config)
    outcome = world.run()
    command = world.launcher.commands[0]
    assert RECORDED_TOKEN not in " ".join(command.argv)
    assert command.env["TIGHTREIN_TOKEN"] == RECORDED_TOKEN
    assert command.env["TIGHTREIN_AUTHZ_MODEL"] == str(world.layout.authz_model(RELEASE))
    assert "--include-method" in command.argv and "--seed" in command.argv
    for path in (tmp_path / "raw").rglob("*"):
        if path.is_file():
            assert RECORDED_TOKEN not in path.read_text(encoding="utf-8")
    assert all(RECORDED_TOKEN not in json.dumps(signal.context) for signal in outcome.signals)


def test_auth_failures_on_endpoints_outside_the_model_are_unjudged(tmp_path, make_config):
    world = World(tmp_path, make_config)
    outcome = world.run()
    checks = sorted(signal.check for signal in outcome.signals if signal.actor["role"] == "Company")
    assert "unauthorized_role_access" in checks
    assert outcome.stats["unjudgedAuthFailures"] == 4 and outcome.stats["expectedDenials"] == 0


def test_partial_when_a_role_cannot_log_in(tmp_path, make_config):
    world = World(tmp_path, make_config, failing={"Admin"})
    outcome = world.run()
    assert outcome.status is RunStatus.PARTIAL and outcome.environment.failed_roles == ("Admin",)
    assert "角色 Admin 登录失败：登录接口返回 401" in outcome.notes
    assert [command.cwd.name for command in world.launcher.commands] == ["Company"]


def test_failed_when_every_role_fails_to_log_in(tmp_path, make_config):
    outcome = World(tmp_path, make_config, failing={"Admin", "Company"}).run()
    assert outcome.status is RunStatus.FAILED and "全部角色登录失败" in outcome.notes
    assert outcome.environment.failed_roles == ("Company", "Admin")


def test_incomplete_report_and_exit_code_two(tmp_path, make_config):
    launcher = FakeSchemathesis({"Company": without("EngineFinished"), "Admin": None}, {"Admin": ToolRun(2)})
    outcome = World(tmp_path, make_config, launcher=launcher).run()
    assert outcome.status is RunStatus.FAILED and outcome.signals == ()
    assert not outcome.environment.report_complete
    assert "角色 Company：报告不完整(没有引擎结束事件)" in outcome.notes
    assert "角色 Admin：Schemathesis 退出码 2(配置或接口描述错误)，见 schemathesis.log" in outcome.notes


def test_one_failed_role_makes_the_run_partial(tmp_path, make_config):
    launcher = FakeSchemathesis({"Company": None}, {"Admin": ToolRun(None, timed_out=True)})
    outcome = World(tmp_path, make_config, launcher=launcher).run(ProbeLevel.DEEP)
    assert outcome.status is RunStatus.PARTIAL and len(outcome.signals) == 6
    assert outcome.coverage.methods == "all" and {item.role for item in outcome.coverage.endpoints} == {"Company"}


def test_skipped_without_a_target(tmp_path, make_config):
    world = World(tmp_path, make_config)
    no_target = world.probe.run(replace(world.target, base_url=None), ProbeLevel.SHALLOW, ProbeOptions())
    assert (no_target.status, no_target.skipped_reason) == (RunStatus.SKIPPED, NO_TARGET)


def test_without_accounts_runs_anonymously(tmp_path, make_config):
    world = World(tmp_path, make_config, launcher=FakeSchemathesis({ANONYMOUS_ROLE: None}))
    config = make_config(accounts=None)
    world.probe.deps = replace(world.probe.deps, config=config,
                               session=Session("https://staging.example.test", None, Credentials(),
                                               LoginTransport(), world.redactor))
    outcome = world.run()
    command = world.launcher.commands[0]
    assert command.cwd.name == ANONYMOUS_ROLE and "TIGHTREIN_TOKEN" not in command.env
    assert "TIGHTREIN_AUTHZ_MODEL" not in command.env
    assert not any(call[0].startswith("authz") for call in world.client.calls)
    toml = tomllib.loads((tmp_path / "raw" / "api-fuzz" / ANONYMOUS_CONFIG_FILE).read_text(encoding="utf-8"))
    assert "headers" not in toml and str(command.argv[2]).endswith(ANONYMOUS_CONFIG_FILE)
    assert outcome.status is RunStatus.OK and ANONYMOUS_ONLY in outcome.notes
    assert {signal.actor["role"] for signal in outcome.signals} == {ANONYMOUS_ROLE}
    assert all("-H 'Authorization" not in signal.context["reproduce"] for signal in outcome.signals)
    assert outcome.stats["unjudgedAuthFailures"] > 0 and outcome.stats["expectedDenials"] == 0
    assert {item.role for item in outcome.coverage.endpoints} == {ANONYMOUS_ROLE}


def test_a_spec_not_from_the_repo_runs_without_a_worktree(tmp_path, make_config):
    world = World(tmp_path, make_config, launcher=FakeSchemathesis({ANONYMOUS_ROLE: None}))
    world.client.repo_needed = False
    world.probe.deps = replace(world.probe.deps, config=make_config(accounts=None),
                               session=Session("https://staging.example.test", None, Credentials(),
                                               LoginTransport(), world.redactor))
    world.target = replace(world.target, worktree=None)
    outcome = world.run()
    assert outcome.status is RunStatus.OK and ANONYMOUS_ONLY in outcome.notes
    assert world.client.calls[0] == ("spec_export", None, RELEASE)


def test_anonymous_alongside_roles_stays_out_of_the_authorization_model(tmp_path, make_config):
    world = World(tmp_path, make_config, launcher=FakeSchemathesis({"Company": None, ANONYMOUS_ROLE: None}))
    outcome = world.run(roles=("Company", ANONYMOUS_ROLE))
    assert ("authz_roles", world.target.worktree, RELEASE, ("Company",)) in world.client.calls
    assert [command.cwd.name for command in world.launcher.commands] == ["Company", ANONYMOUS_ROLE]
    assert outcome.status is RunStatus.OK
    model = authz_model.load(world.layout.authz_model(RELEASE))
    assert ANONYMOUS_ROLE not in model.roles
    assert hooks.violation(model, ANONYMOUS_ROLE, "GET", "/api/User/List", 200) is None


ACCESS_CODE = "code-7f3a9c41"


class CodeSchemathesis(FakeSchemathesis):
    """另在角色目录写出一份带凭证请求头的记录，检查调用结束后被清除。"""

    def __call__(self, command):
        run = super().__call__(command)
        (Path(command.cwd) / "headers.json").write_text(
            json.dumps({"X-Access-Code": command.env["TIGHTREIN_TOKEN"]}), encoding="utf-8")
        return run


class CodeCredentials:
    def account(self, role):
        return role

    def password(self, role):
        return ACCESS_CODE


def test_static_header_credentials(tmp_path, make_config):
    world = World(tmp_path, make_config, launcher=CodeSchemathesis({"Company": None}))
    config = make_config(accounts={"roles": {"Company": ROLES_CONFIG["Company"]},
                                   "login": {"kind": "static-header", "header": "X-Access-Code"}})
    world.probe.deps = replace(world.probe.deps, config=config, session=Session(
        "https://staging.example.test", LoginSettings.from_config(config), CodeCredentials(), LoginTransport(),
        world.redactor))
    outcome = world.run()
    command = world.launcher.commands[0]
    assert command.env["TIGHTREIN_TOKEN"] == ACCESS_CODE and ACCESS_CODE not in " ".join(command.argv)
    toml = tomllib.loads((tmp_path / "raw" / "api-fuzz" / "schemathesis.toml").read_text(encoding="utf-8"))
    assert toml["headers"] == {"X-Access-Code": "${TIGHTREIN_TOKEN}"}
    assert "x-access-code" in toml["output"]["sanitization"]["keys-to-sanitize"]
    for path in (tmp_path / "raw").rglob("*"):
        if path.is_file():
            assert ACCESS_CODE not in path.read_text(encoding="utf-8")
    assert outcome.signals and all(ACCESS_CODE not in json.dumps(signal.context) for signal in outcome.signals)
    assert "-H 'X-Access-Code: <TOKEN>'" in outcome.signals[0].context["reproduce"]


def test_skipped_and_failed_before_running(tmp_path, make_config):
    no_spec = FakeClient(spec_export=default(ExtensionPoint.SPEC_EXPORT, ()))
    skipped = World(tmp_path / "a", make_config, client=no_spec).run()
    assert skipped.status is RunStatus.SKIPPED and skipped.skipped_reason == "未提供 spec-export 扩展"
    broken = FakeClient(spec_export=error(ExtensionPoint.SPEC_EXPORT))
    assert World(tmp_path / "b", make_config, client=broken).run().status is RunStatus.FAILED
    missing = World(tmp_path / "c", make_config, installed=lambda: "Schemathesis 未安装；重新安装").run()
    assert (missing.status, missing.notes) == (RunStatus.FAILED, ("Schemathesis 未安装；重新安装",))


def test_failed_authorization_extensions_make_the_run_partial(tmp_path, make_config):
    spec_path = write_spec(tmp_path / "specs")
    client = FakeClient(spec_export=spec_result(spec_path), authz_endpoints=error(ExtensionPoint.AUTHZ_ENDPOINTS),
                        authz_roles=ok(ExtensionPoint.AUTHZ_ROLES, ROLES))
    world = World(tmp_path, make_config, client=client)
    outcome = world.run()
    assert outcome.status is RunStatus.PARTIAL
    assert "TIGHTREIN_AUTHZ_MODEL" not in world.launcher.commands[0].env
    assert outcome.stats["unjudgedAuthFailures"] == 4
    assert any(note.startswith("authz-endpoints 失败，不做越权检查") for note in outcome.notes)


def test_from_raw_parses_existing_reports_without_tools(tmp_path, make_config):
    world = World(tmp_path, make_config)
    write_events(tmp_path / "raw" / "api-fuzz" / "Company" / "events.ndjson")
    path = world.layout.authz_model(RELEASE)
    path.write_text(json.dumps(stub_model().to_document("2026-10-05T03:00:00Z")), encoding="utf-8")
    outcome = world.run(from_raw=True)
    assert world.launcher.commands == [] and world.client.calls == []
    assert outcome.status is RunStatus.PARTIAL and len(outcome.signals) == 6
    assert outcome.stats["expectedDenials"] == 2
    assert "角色 Admin 没有已有的报告" in outcome.notes and outcome.coverage.endpoints_total == 6


def test_scrub_tokens(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.ndjson").write_bytes(b'{"Authorization": "Bearer tok-123"}')
    (tmp_path / "b.bin").write_bytes(b"\x00\x01")
    scrub_tokens(tmp_path, ["tok-123", ""])
    assert (tmp_path / "a" / "x.ndjson").read_text(encoding="utf-8") == '{"Authorization": "Bearer [已脱敏]"}'
    assert (tmp_path / "b.bin").read_bytes() == b"\x00\x01"


@pytest.mark.parametrize("roles", [("Company",)])
def test_roles_option_limits_the_run(tmp_path, make_config, roles):
    world = World(tmp_path, make_config)
    outcome = world.run(roles=roles, include_paths=("/api/User/List",))
    assert [command.cwd.name for command in world.launcher.commands] == ["Company"]
    assert "/api/User/List" in world.launcher.commands[0].argv and outcome.status is RunStatus.OK
