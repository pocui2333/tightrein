import json
from types import SimpleNamespace

import pytest
from probe_world import make_redactor, make_target

from tightrein.domain.enums import ProbeLevel, RegressionKind, RegressionResult, RunStatus
from tightrein.sources.base import ProbeOutcome
from tightrein.sources.common.http import HttpResponse
from tightrein.sources.common.procs import ToolRun
from tightrein.sources.common.session import LoginFailed
from tightrein.pipeline.checks.pages import plan
from tightrein.pipeline.checks.pages.runner import PageRun
from tightrein.pipeline.checks.regressions import manifest
from tightrein.pipeline.checks.regressions.api_check import ApiCheck
from tightrein.pipeline.checks.regressions.manifest import ManifestInvalid, ManifestTampered
from tightrein.pipeline.checks.project_checks import CheckCommand, repro_test_cwd
from tightrein.pipeline.checks.regressions.page_check import PageCheck
from tightrein.pipeline.checks.regressions.repo_test_check import RepoTestCheck
from tightrein.pipeline.checks.regressions.runner import Execution, RegressionExecutor
from tightrein.pipeline.checks.regressions.static_check import StaticCheck
from tightrein.store.files import yaml_text
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos.regressions import RegressionCheck

FINGERPRINT = "a1b2c3d4e5f60718"
REQUEST = {"method": "GET", "path": "/api/orders/42", "pathTemplate": "/api/orders/{id}", "query": {}, "body": None}
API = {"id": "api-1", "kind": "api", "role": "Admin", "file": "api-1.request.json", "location": "GET /api/orders/{id}",
       "requires": ["backend"], "precondition": None}
PAGE = {"id": "page-1", "kind": "page", "role": "Admin", "file": "page-1.spec.ts", "location": "/orders",
        "requires": ["backend", "frontend"], "precondition": None}
STATIC = {"id": "static-1", "kind": "static", "role": None, "file": "static-1.yaml", "location": "src/order.src:12",
          "targets": ["src/order.src"], "requires": [], "precondition": None}
PYTEST = "python -m pytest -q"
TEST = {"id": "test-1", "kind": "test", "file": "tests/test_order.py", "location": "src/order.py:12",
        "command": f"{PYTEST} tests/test_order.py::test_owner"}


def write_checks(layout, issue_id="0007", entries=(API,), files=None):
    directory = layout.regression_dir(issue_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / manifest.CHECKLIST).write_text(yaml_text.dump(
        {"issue": issue_id, "problems": [FINGERPRINT], "checks": list(entries)}), encoding="utf-8")
    defaults = {"api-1.request.json": json.dumps(REQUEST),
                "api-1.expect.json": json.dumps({"status": {"notIn": ["5xx"]}}),
                "page-1.spec.ts": "test('orders', async () => {});\n", "static-1.yaml": "rules: []\n",
                "test-1.py": "def test_owner():\n    assert False\n"}
    for name, text in {**defaults, **(files or {})}.items():
        (directory / name).write_text(text, encoding="utf-8")
    loaded = manifest.load(directory)
    return [RegressionCheck(issue_id, entry.id, entry.kind, f"regressions/{issue_id}/check.yaml",
                            manifest.entry_hash(directory, entry)) for entry in loaded.checks]


class FakeSession:
    def __init__(self, fail=False):
        self.fail = fail

    def login(self, role):
        if self.fail:
            raise LoginFailed(role, "密码错误")
        return SimpleNamespace(token="token-1")

    def headers(self, role):
        return {"Authorization": f"Bearer {self.login(role).token}"}


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, request):
        self.urls.append(request.url)
        return self.responses.pop(0)


@pytest.fixture
def layout(tmp_path):
    return WorkspaceLayout(tmp_path / "ws")


def api_executor(transport, session=None):
    return ApiCheck(session or FakeSession(), transport, make_redactor(), 30)


def test_manifest_validation_and_missing_files(layout):
    write_checks(layout)
    directory = layout.regression_dir("0007")
    (directory / "api-1.expect.json").unlink()
    with pytest.raises(ManifestInvalid, match="api-1.expect.json"):
        manifest.load(directory)
    (directory / manifest.CHECKLIST).write_text("issue: '0007'\nproblems: []\nchecks: []\n", encoding="utf-8")
    with pytest.raises(ManifestInvalid):
        manifest.load(directory)


def test_invalid_manifests_are_reported_per_check_and_tampering_raises(layout, tmp_path):
    checks = write_checks(layout)
    (layout.regression_dir("0007") / "api-1.request.json").write_text(json.dumps({**REQUEST, "path": "/x"}))
    executor = RegressionExecutor(layout, api=api_executor(Transport()))
    with pytest.raises(ManifestTampered):
        executor.run_checks(checks, make_target(tmp_path))
    (layout.regression_dir("0007") / manifest.CHECKLIST).unlink()
    [outcome] = executor.run_checks(checks, make_target(tmp_path))
    assert outcome.result is RegressionResult.INVALID and "没有清单" in outcome.detail


@pytest.mark.parametrize("expect,status,body,result", [
    ({"status": {"notIn": ["5xx"]}}, 500, b"", RegressionResult.FAILED),
    ({"status": {"notIn": ["5xx"]}}, 400, b"", RegressionResult.PASSED),
    ({"status": {"in": [401, 403, 404]}}, 200, b"{}", RegressionResult.FAILED),
    ({"status": {"in": [401, 403, 404]}}, 403, b"", RegressionResult.PASSED),
    ({"status": {"equals": 200}, "bodySchema": {"type": "object", "required": ["id"]}}, 200, b'{"name": 1}',
     RegressionResult.FAILED),
    ({"status": {"equals": 200}, "bodySchema": {"type": "object", "required": ["id"]}}, 200, b'{"id": 1}',
     RegressionResult.PASSED),
])
def test_api_expectations(layout, tmp_path, expect, status, body, result):
    checks = write_checks(layout, files={"api-1.expect.json": json.dumps(expect)})
    transport = Transport(HttpResponse(status, body))
    [outcome] = RegressionExecutor(layout, api=api_executor(transport)).run_checks(checks, make_target(tmp_path))
    assert outcome.result is result and outcome.location == "GET /api/orders/{id}"
    assert transport.urls == ["https://staging.example.test/api/orders/42"]


def test_unmet_preconditions_are_marked_and_unreachable_targets_are_not_run(layout, tmp_path):
    checks = write_checks(layout, files={"api-1.precondition.json": json.dumps({**REQUEST, "path": "/api/orders"})})
    transport = Transport(HttpResponse(200, b"[]"), HttpResponse(400, b""))
    [outcome] = RegressionExecutor(layout, api=api_executor(transport)).run_checks(checks, make_target(tmp_path))
    assert (outcome.result, outcome.precondition_met) == (RegressionResult.PASSED, False)
    refused = HttpResponse(None, error="refused")
    executor = RegressionExecutor(layout, api=api_executor(Transport(refused, refused)))
    assert executor.run_checks(checks, make_target(tmp_path))[0].result is RegressionResult.NOT_RUN
    executor = RegressionExecutor(layout, api=api_executor(Transport(), FakeSession(fail=True)))
    [outcome] = executor.run_checks(checks, make_target(tmp_path))
    assert outcome.result is RegressionResult.NOT_RUN and "登录失败" in outcome.detail
    assert RegressionExecutor(layout).run_checks(checks, make_target(tmp_path))[0].result is RegressionResult.NOT_RUN


class Launcher:
    def __init__(self, run):
        self.run = run
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        return self.run


@pytest.mark.parametrize("run,result", [
    (ToolRun(0, json.dumps({"results": [], "errors": []})), RegressionResult.PASSED),
    (ToolRun(0, json.dumps({"results": [{"check_id": "r", "path": "src/order.src", "start": {"line": 12},
                                         "extra": {"message": "m"}}], "errors": []})), RegressionResult.FAILED),
    (ToolRun(2, "", "boom"), RegressionResult.NOT_RUN),
])
def test_static_checks(layout, tmp_path, run, result):
    checks = write_checks(layout, entries=(STATIC,))
    launcher = Launcher(run)
    executor = RegressionExecutor(layout, static=StaticCheck(launcher, {}, 60))
    [outcome] = executor.run_checks(checks, make_target(tmp_path, worktree=tmp_path / "wt"))
    assert outcome.result is result
    assert launcher.commands[0].argv[-1] == "src/order.src" and launcher.commands[0].cwd == tmp_path / "wt"
    assert executor.run_checks(checks, make_target(tmp_path))[0].result is RegressionResult.NOT_RUN


def test_static_checks_can_reference_a_patrol_rule(layout, tmp_path):
    reference = yaml_text.dump({"configs": ["p/demo"], "rule": "demo.unchecked-id"})
    checks = write_checks(layout, entries=(STATIC,), files={"static-1.yaml": reference})
    other = {"check_id": "demo.other", "path": "src/order.src", "start": {"line": 3}, "extra": {"message": "m"}}
    launcher = Launcher(ToolRun(0, json.dumps({"results": [other], "errors": []})))
    executor = RegressionExecutor(layout, static=StaticCheck(launcher, {}, 60))
    [outcome] = executor.run_checks(checks, make_target(tmp_path, worktree=tmp_path / "wt"))
    assert outcome.result is RegressionResult.PASSED
    assert launcher.commands[0].argv[:4] == ("semgrep", "scan", "--config", "p/demo")
    custom = Launcher(ToolRun(0, json.dumps({"results": [], "errors": []})))
    RegressionExecutor(layout, static=StaticCheck(custom, {}, 60, "/opt/semgrep")).run_checks(
        checks, make_target(tmp_path, worktree=tmp_path / "wt"))
    assert custom.commands[0].argv[:2] == ("/opt/semgrep", "scan")


class FakePages:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def run(self, target, *, roles=(), spec_dirs=(), grep=None):
        self.calls.append((roles, spec_dirs, grep))
        results = target.raw_dir / plan.RESULTS_FILE
        results.parent.mkdir(parents=True, exist_ok=True)
        results.write_text("\n".join(json.dumps({"title": "orders", "file": f"/abs/{name}", "project": "regress-Admin",
                                                 "role": "Admin", "outcome": outcome})
                                     for name, outcome in self.outcome.items()) + "\n", encoding="utf-8")
        return PageRun(RunStatus.OK)


@pytest.mark.parametrize("case,result,met", [
    ("expected", RegressionResult.PASSED, True), ("unexpected", RegressionResult.FAILED, True),
    ("skipped", RegressionResult.NOT_RUN, False),
])
def test_page_checks_run_once_per_issue(layout, tmp_path, case, result, met):
    checks = write_checks(layout, entries=(PAGE,))
    pages = FakePages({"page-1.spec.ts": case})
    [outcome] = RegressionExecutor(layout, page=PageCheck(pages)).run_checks(checks, make_target(tmp_path))
    assert (outcome.result, outcome.precondition_met) == (result, met)
    roles, spec_dirs, grep = pages.calls[0]
    assert spec_dirs == (layout.regression_dir("0007"),) and roles == ("Admin",) and grep == "."


def test_a_page_case_matches_its_spec_file_by_whole_name(layout, tmp_path):
    checks = write_checks(layout, entries=(PAGE,))
    pages = FakePages({"other-page-1.spec.ts": "unexpected"})
    [outcome] = RegressionExecutor(layout, page=PageCheck(pages)).run_checks(checks, make_target(tmp_path))
    assert outcome.result is RegressionResult.NOT_RUN


def test_checks_are_dispatched_by_kind_in_the_given_order(layout, tmp_path):
    checks = write_checks(layout, entries=(STATIC, API))
    calls = []

    def api(entry, directory, target):
        calls.append(entry.id)
        return Execution(RegressionResult.PASSED)

    def static(entry, directory, worktree, raw_dir):
        calls.append(entry.id)
        return Execution(RegressionResult.FAILED, "命中")

    outcomes = RegressionExecutor(layout, api=api, static=static).run_checks(
        checks, make_target(tmp_path, worktree=tmp_path / "wt"))
    assert [(item.check.check_id, item.check.kind, item.result) for item in outcomes] == [
        ("static-1", RegressionKind.STATIC, RegressionResult.FAILED),
        ("api-1", RegressionKind.API, RegressionResult.PASSED)]
    assert calls == ["static-1", "api-1"]


def test_entries_of_the_test_kind(layout):
    write_checks(layout, entries=(TEST, STATIC))
    loaded = manifest.load(layout.regression_dir("0007"))
    test, static = loaded.checks
    assert (test.stored_name, test.files(), test.requires, test.code_paths()) == (
        "test-1.py", ("test-1.py",), (), ("src/order.py",))
    assert static.code_paths() == ("src/order.src",) and "command" not in static.to_dict()
    with pytest.raises(ManifestInvalid, match="仓库内的相对路径"):
        write_checks(layout, entries=({**TEST, "file": "../outside/test_x.py"},))
    with pytest.raises(ManifestInvalid):
        write_checks(layout, entries=({key: value for key, value in TEST.items() if key != "command"},))


def test_the_command_must_come_from_the_project_checks():
    commands = [CheckCommand("pytest", ".", "python -m pytest -q"), CheckCommand("js", "web", "node --test tests/js/")]
    assert repro_test_cwd(commands, f"{PYTEST} tests/test_order.py::test_owner", "tests/test_order.py") == "."
    assert repro_test_cwd(commands, "node --test tests/js/a.test.js", "web/tests/js/a.test.js") == "web"
    assert repro_test_cwd(commands, "python -c print(1) tests/test_order.py", "tests/test_order.py") is None
    assert repro_test_cwd(commands, f"{PYTEST} tests/other.py", "tests/test_order.py") is None


@pytest.mark.parametrize("run,result", [
    (ToolRun(0), RegressionResult.PASSED), (ToolRun(1), RegressionResult.FAILED),
    (ToolRun(2), RegressionResult.INVALID), (ToolRun(5), RegressionResult.INVALID),
    (ToolRun(None, start_error="not found"), RegressionResult.NOT_RUN),
    (ToolRun(None, timed_out=True), RegressionResult.NOT_RUN),
])
def test_test_checks(layout, tmp_path, run, result):
    checks = write_checks(layout, entries=(TEST,))
    worktree = tmp_path / "wt"
    (worktree / "tests").mkdir(parents=True)
    (worktree / "tests" / "test_order.py").write_text("def test_owner(): ...\n", encoding="utf-8")
    launcher = Launcher(run)
    commands = [CheckCommand("pytest", ".", "python -m pytest -q")]
    check = RepoTestCheck(launcher, {}, 60, lambda command, file: repro_test_cwd(commands, command, file), (1,))
    executor = RegressionExecutor(layout, test=check)
    [outcome] = executor.run_checks(checks, make_target(tmp_path, worktree=worktree))
    assert outcome.result is result
    assert launcher.commands[0].argv[-1] == "tests/test_order.py::test_owner" and launcher.commands[0].cwd == worktree
    (worktree / "tests" / "test_order.py").unlink()
    [missing] = executor.run_checks(checks, make_target(tmp_path, worktree=worktree))
    assert missing.result is RegressionResult.NOT_RUN and "不在 worktree 中" in missing.detail
    refused = RepoTestCheck(launcher, {}, 60, lambda command, file: None, (1,))
    [outcome] = RegressionExecutor(layout, test=refused).run_checks(checks, make_target(tmp_path, worktree=worktree))
    assert outcome.result is RegressionResult.INVALID and "checks.commands" in outcome.detail
