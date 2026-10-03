import json
from datetime import datetime, timezone

import pytest
from extension_world import COMMAND, PYTHON

from tightrein.config.project import MissingSetting
from tightrein.domain.clock import FixedClock
from tightrein.domain.enums import ExtensionErrorCode, ExtensionLayer, ExtensionPoint, Probe, ProbeLevel
from tightrein.extensions import defaults
from tightrein.extensions.cache import ExtensionCache
from tightrein.extensions.client import (
    MODE_API,
    MODE_PAGE,
    ExtensionClient,
    StaticScope,
    WorktreeNotAtCommit,
    local_run_ports,
)
from tightrein.extensions.invoke import Invoker
from tightrein.extensions.resolve import resolve
from tightrein.observability import events
from tightrein.observability.events import EventLog
from tightrein.observability.redact import Redactor
from tightrein.observability.tracing import Tracer

RUN = "R-20260929-021503-collect-api-fuzz"
COMMIT = "d6f37025c3b1e0a9f8e7d6c5b4a39281706f5e4d"
NOW = datetime(2026, 9, 29, 2, 15, tzinfo=timezone.utc)
DOCUMENT = {"openapi": "3.0.1", "paths": {"/api/Order/Query": {"post": {}}}}
ROLES = {"capabilities": ["OrderRead"], "roles": {"Company": {"OrderRead": True}}, "sourceFiles": []}
SERVICE = {"name": "backend", "argv": ["dotnet", "run"], "cwd": ".", "env": {"APP_ENVIRONMENT": "Development"},
           "port": 5100, "readyUrl": None, "readyPatterns": ["Now listening on"], "failPatterns": [], "after": None}
PLAN = {"services": [SERVICE], "unavailable": [], "migrationPaths": []}


class Heads:
    def __init__(self, commit=COMMIT):
        self.commit = commit
        self.asked = []

    def __call__(self, repo):
        self.asked.append(repo)
        return self.commit


def client_for(world, extensions, heads=None, **config_changes):
    world.project_extension()
    config = world.config(extensions=extensions, **config_changes)
    resolution = resolve(config, world.workspace, world.tool, python=PYTHON)
    tracer = Tracer(EventLog(world.workspace, Redactor()), FixedClock(NOW), run_id=RUN, stage="collect")
    invoker = Invoker(world.workspace, world.user, run_id=RUN, environ={"PATH": "/usr/bin:/bin"}, tracer=tracer,
                      scratch_root=world.root)
    return ExtensionClient(config, resolution, invoker, ExtensionCache(world.workspace, FixedClock(NOW)),
                           head=heads or Heads())


def extension(options, **fields):
    return {"command": COMMAND, "options": options, **fields}


def recorded(path):
    return json.loads(path.read_text(encoding="utf-8"))["request"]


def test_spec_export_writes_the_document_and_is_cached_by_commit(world):
    record = world.root / "record.json"
    log = world.root / "export.log"
    options = {"behavior": "spec", "document": DOCUMENT, "logFile": str(log), "record": str(record)}
    client = client_for(world, {"spec-export": extension(options)})
    repo = world.root / "readonly"
    result = client.spec_export(repo, COMMIT)
    openapi = world.workspace.openapi(COMMIT)
    assert (result.implementation, result.failure, result.cached) == (ExtensionLayer.PROJECT, None, False)
    assert result.output["specFile"] == str(openapi.absolute())
    assert json.loads(openapi.read_text(encoding="utf-8")) == DOCUMENT
    assert recorded(record)["input"] == {"outputFile": str(openapi.absolute())}
    assert world.workspace.spec_export_log(RUN).read_text(encoding="utf-8") == "build succeeded\n"
    record.unlink()
    again = client.spec_export(repo, COMMIT)
    assert (again.output, again.cached) == (result.output, True)
    assert not record.exists()
    written = events.read(world.workspace.events_log(NOW.date()))
    assert [event.attributes["cached"] for event in written] == [False, True]


def test_a_cached_spec_needs_the_document_file(world):
    options = {"behavior": "spec", "document": DOCUMENT}
    client = client_for(world, {"spec-export": extension(options)})
    client.spec_export(world.root, COMMIT)
    world.workspace.openapi(COMMIT).unlink()
    result = client.spec_export(world.root, COMMIT)
    assert (result.cached, result.failure) == (False, None)
    assert world.workspace.openapi(COMMIT).is_file()


@pytest.mark.parametrize("options, message", [
    ({"behavior": "spec", "document": {"openapi": "3.0.1"}}, "中没有 paths"),
    ({"behavior": "spec", "document": DOCUMENT, "specFile": "/tmp/other.json"}, "应等于 outputFile"),
])
def test_spec_export_output_is_checked_beyond_the_schema(world, options, message):
    client = client_for(world, {"spec-export": extension(options)})
    result = client.spec_export(world.root, COMMIT)
    assert result.failure.code is ExtensionErrorCode.SCHEMA_INVALID
    assert message in result.failure.message
    assert not world.workspace.extension_meta(COMMIT, ExtensionPoint.SPEC_EXPORT).exists()


def test_a_cache_miss_needs_the_worktree_at_the_commit(world):
    record = world.root / "record.json"
    heads = Heads("0" * 40)
    client = client_for(world, {"authz-roles": extension({"output": ROLES, "record": str(record)})}, heads)
    with pytest.raises(WorktreeNotAtCommit) as caught:
        client.authz_roles(world.root, COMMIT, ["Company"])
    assert (caught.value.commit, caught.value.head) == (COMMIT, "0" * 40)
    assert not record.exists()
    heads.commit = COMMIT
    assert client.authz_roles(world.root, COMMIT, ["Company"]).output == ROLES
    assert recorded(record)["input"] == {"roles": ["Company"]}
    heads.commit = "0" * 40
    assert client.authz_roles(world.root, COMMIT, ["Company"]).cached


def test_a_workspace_spec_file_needs_no_worktree(world):
    world.workspace.root.mkdir(parents=True, exist_ok=True)
    (world.workspace.root / "openapi.yaml").write_text("openapi: 3.0.1\npaths:\n  /api/ping: {get: {}}\n",
                                                       encoding="utf-8")
    heads = Heads("0" * 40)
    spec = {"use": "core/openapi-file", "options": {"path": "openapi.yaml", "base": "workspace"}}
    client = client_for(world, {"spec-export": spec}, heads)
    assert not client.needs_repo(ExtensionPoint.SPEC_EXPORT)
    result = client.spec_export(None, COMMIT)
    assert result.failure is None and result.output["specFile"] == str(world.workspace.openapi(COMMIT).absolute())
    assert heads.asked == []
    repo_spec = {"use": "core/openapi-file", "options": {"path": "openapi.yaml"}}
    other = client_for(world, {"spec-export": repo_spec}, heads)
    assert other.needs_repo(ExtensionPoint.SPEC_EXPORT)
    with pytest.raises(WorktreeNotAtCommit, match="没有只读 worktree"):
        other.spec_export(None, "1" * 40)


def test_points_without_an_extension_return_the_default_without_reading_head(world):
    heads = Heads()
    client = client_for(world, {}, heads)
    assert client.spec_export(world.root, COMMIT) == defaults.result(ExtensionPoint.SPEC_EXPORT)
    assert client.page_routes(world.root, COMMIT) == defaults.result(ExtensionPoint.PAGE_ROUTES)
    since = datetime(2026, 9, 29, 2, 0, tzinfo=timezone.utc)
    assert client.error_tracking(since, NOW) == defaults.result(ExtensionPoint.ERROR_TRACKING)
    assert client.log_platform("{app=\"x\"}", since, NOW, 10) == defaults.result(ExtensionPoint.LOG_PLATFORM)
    assert client.alert_source() == defaults.result(ExtensionPoint.ALERT_SOURCE)
    assert client.method(ExtensionPoint.SPEC_EXPORT) is None
    assert client.local_run(world.root, MODE_API, {"backend": 5100, "frontend": None}) == defaults.result(
        ExtensionPoint.LOCAL_RUN)
    assert heads.asked == []


def test_failures_are_not_cached(world):
    options = {"behavior": "error", "code": "build-failed", "message": "构建失败"}
    client = client_for(world, {"authz-endpoints": extension(options)})
    assert client.authz_endpoints(world.root, COMMIT).failure.code is ExtensionErrorCode.BUILD_FAILED
    assert not world.workspace.extension_output(COMMIT, ExtensionPoint.AUTHZ_ENDPOINTS).exists()


def test_platform_points_send_the_window_without_a_repo(world):
    record = world.root / "record.json"
    output = {"chunks": [], "truncated": False, "oldestAvailable": None}
    extensions = {"log-platform": extension({"output": output, "record": str(record)})}
    client = client_for(world, extensions)
    since = datetime(2026, 9, 29, 2, 0, tzinfo=timezone.utc)
    assert client.log_platform('{app="api"}', since, NOW, 500).output == output
    assert recorded(record)["input"] == {"query": '{app="api"}', "since": "2026-09-29T02:00:00Z",
                                         "until": "2026-09-29T02:15:00Z", "limit": 500}
    assert (recorded(record)["repo"], recorded(record)["commit"]) == (None, None)
    alerts = {"alert-source": extension({"output": {"alerts": []}, "record": str(record)})}
    assert client_for(world, alerts).alert_source().output == {"alerts": []}
    assert recorded(record)["input"] == {}


def test_log_parse_passes_chunks_and_state(world):
    record = world.root / "record.json"
    chunk = {"stream": "app.log", "text": "x\n", "startPosition": 0, "endPosition": 2, "modifiedAt": None}
    output = {"entries": [], "state": {"app.log": "02:15:03"}, "unparsed": 1}
    client = client_for(world, {"log-parse": extension({"output": output, "record": str(record)})})
    assert client.log_parse([chunk], None).output == output
    assert recorded(record)["input"] == {"chunks": [chunk], "state": None}


def test_static_tools_creates_the_raw_directory(world):
    record = world.root / "record.json"
    output = {"tools": [], "findings": []}
    client = client_for(world, {"static-tools": extension({"output": output, "record": str(record)})})
    raw_dir = world.workspace.probe_raw_dir(RUN, Probe.STATIC)
    scope = StaticScope(ProbeLevel.INCREMENTAL, "a1b2c3d", ("src/A.cs",))
    assert client.static_tools(world.root, COMMIT, scope, raw_dir).output == output
    assert raw_dir.is_dir()
    assert recorded(record)["input"] == {"level": "incremental", "baseCommit": "a1b2c3d",
                                          "changedFiles": ["src/A.cs"], "rawDir": str(raw_dir.absolute())}


def test_local_run_plans_must_not_carry_credentials(world):
    unsafe = {**PLAN, "services": [{**SERVICE, "env": {"API_TOKEN": "x"}}]}
    client = client_for(world, {"local-run": extension({"output": unsafe})})
    result = client.local_run(world.root, MODE_API, {"backend": 5100, "frontend": None})
    assert result.failure.code is ExtensionErrorCode.SCHEMA_INVALID
    assert "API_TOKEN" in result.failure.message
    record = world.root / "record.json"
    client = client_for(world, {"local-run": extension({"output": PLAN, "record": str(record)})})
    assert client.local_run(world.root, MODE_PAGE, {"backend": 5000, "frontend": 8080}).output == PLAN
    assert recorded(record)["commit"] == COMMIT
    assert recorded(record)["input"] == {"mode": "page", "ports": {"backend": 5000, "frontend": 8080}}


def test_local_run_ports_come_from_the_project_config(world):
    config = world.config(localRun={"ports": {"api": 5100, "backendForPages": 5000, "frontend": 8080}})
    assert local_run_ports(config, MODE_API) == {"backend": 5100, "frontend": None}
    assert local_run_ports(config, MODE_PAGE) == {"backend": 5000, "frontend": 8080}
    with pytest.raises(ValueError):
        local_run_ports(config, "desktop")
    assert local_run_ports(world.config(), MODE_API) == {"backend": None, "frontend": None}


def test_local_run_requires_ports_only_when_an_extension_plans_the_start(world, tmp_path):
    client_for(world, {}).local_run(world.root, MODE_API, {"backend": None, "frontend": None})
    client = client_for(world, {"local-run": extension({"output": PLAN, "record": str(tmp_path / "r.json")})})
    with pytest.raises(MissingSetting):
        client.local_run(world.root, MODE_API, {"backend": None, "frontend": None})
