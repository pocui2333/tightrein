import json

import pytest
from fix_world import SERVICE_PATH, make_fix_world
from pipeline_world import RELEASE, make_signal

from tightrein.domain.enums import Complexity, ImpactKind, Probe, RegressionKind
from tightrein.pipeline.fix.steps import context, repro, repro_test, workspace
from tightrein.pipeline.fix.steps.workspace import BranchRejected
from tightrein.pipeline.checks.project_checks import CheckCommand
from tightrein.pipeline.checks.regressions import manifest
from tightrein.store.repos import regressions

REQUEST = {"method": "GET", "path": "/api/Order/42", "pathTemplate": "/api/Order/{id}", "query": {}}


def test_branch_names_follow_the_project_conventions(tmp_path, make_config):
    from dataclasses import replace

    from datetime import datetime, timezone

    from tightrein.domain.enums import IssueStatus, Severity, TaskType, Treatment
    from tightrein.domain.issue import Issue
    from tightrein.pipeline.common.conventions import resolve

    config = make_config()
    generic = resolve(config, tmp_path)
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)
    bug = Issue(id="0007", slug="order-owner-check", title="t", status=IssueStatus.TODO, severity=Severity.P2,
                created_at=now, updated_at=now, task_type=TaskType.BUG, treatment=Treatment.SCHEDULED)
    taken = {"bugfix/7-order-owner-check"}
    assert workspace.branch_name(bug, None, generic, config, exists=taken.__contains__) == \
        "bugfix/7-order-owner-check-2"
    assert workspace.branch_name(bug, "cty", generic, config, exists=taken.__contains__,
                                 current="bugfix/7-order-owner-check") == "bugfix/7-order-owner-check"
    urgent = replace(bug, treatment=Treatment.IMMEDIATE, severity=Severity.P0)
    assert workspace.branch_name(urgent, None, generic, config, exists=lambda name: False) == \
        "hotfix/7-order-owner-check"
    feature = replace(bug, task_type=TaskType.FEATURE)
    personal = replace(generic, personal_prefix=True)
    assert workspace.branch_name(feature, "cty", personal, config, exists=lambda name: False) == \
        "cty/feature/7-order-owner-check"
    with pytest.raises(BranchRejected, match="AI 或工具名称"):
        workspace.branch_name(bug, "Claude", personal, config, exists=lambda name: False)
    with pytest.raises(BranchRejected, match="branchPrefix"):
        workspace.branch_name(bug, None, personal, config, exists=lambda name: False)


def test_context_reads_the_issue_and_the_triage_outputs(tmp_path):
    world = make_fix_world(tmp_path)
    found = context.load(world.conn, world.layout, world.issue_id)
    assert found.issue.title == "订单查询返回 500"
    assert found.acceptance[-1] == "本 Issue 的复现检查在修复后通过"
    assert (found.complexity, found.impact_kind, found.root_files) == (
        Complexity.LOW, ImpactKind.NON_CORE_ERROR, (SERVICE_PATH,))
    assert found.flags == {"design": False, "dataStructure": False, "publicContract": False}
    assert "## 原因" in found.issue_text() and found.verify_report is None
    assert context.budget(world.config, Complexity.LOW) is None


def api_signal(check="not_a_server_error"):
    return make_signal(1, check=check, context={"request": REQUEST, "response": {"status": 500}})


@pytest.mark.parametrize("check,expect", [
    ("not_a_server_error", {"status": {"notIn": ["5xx"]}}),
    ("unauthorized_role_access", {"status": {"in": [401, 403, 404]}}),
])
def test_api_fuzz_problems_replay_the_request(tmp_path, check, expect):
    world = make_fix_world(tmp_path, signal=api_signal(check))
    found = repro.generate(world.conn, world.layout, world.config, world.issue_id, ["P-0001"], ["p-0001"],
                           {"api": ("backend",)})
    assert found.source == Probe.API_FUZZ.value
    [check_item] = found.manifest["checks"]
    assert (check_item["role"], check_item["requires"], check_item["location"]) == ("Admin", ["backend"],
                                                                                   "GET /api/Order/42")
    assert json.loads(found.files["api-1.expect.json"]) == expect
    assert json.loads(found.files["api-1.request.json"])["pathTemplate"] == "/api/Order/{id}"


def test_contract_problems_expect_the_documented_schema(tmp_path):
    world = make_fix_world(tmp_path, signal=api_signal("status_code_conformance"))
    assert repro.generate(world.conn, world.layout, world.config, world.issue_id, ["P-0001"], ["x"], {}) is None
    spec = {"paths": {"/api/Order/{id}": {"get": {"responses": {"200": {"content": {"application/json": {
        "schema": {"$ref": "#/components/schemas/Order"}}}}}}}}, "components": {"schemas": {"Order": {}}}}
    world.layout.openapi(RELEASE).parent.mkdir(parents=True)
    world.layout.openapi(RELEASE).write_text(json.dumps(spec), encoding="utf-8")
    found = repro.generate(world.conn, world.layout, world.config, world.issue_id, ["P-0001"], ["x"], {})
    assert json.loads(found.files["api-1.expect.json"])["bodySchema"]["components"] == {"schemas": {"Order": {}}}


def test_platform_problems_have_no_deterministic_check(tmp_path):
    signal = make_signal(1, probe=Probe.PLATFORM_ERRORS, check="error", location="OrderService.Get",
                         context={"platformGroup": "sentry:acme/1"})
    world = make_fix_world(tmp_path, signal=signal)
    assert repro.generate(world.conn, world.layout, world.config, world.issue_id, ["P-0001"], ["x"], {}) is None


def test_static_problems_reference_the_patrol_rule(tmp_path):
    finding = {"tool": "semgrep", "rule": "demo.unchecked-id", "file": SERVICE_PATH, "line": 12}
    signal = make_signal(1, probe=Probe.STATIC, check="demo.unchecked-id", location=f"{SERVICE_PATH}:12",
                         context={"toolFinding": finding})
    world = make_fix_world(tmp_path, signal=signal, sources={"static": {"semgrep": {"configs": ["p/demo"]}}})
    found = repro.generate(world.conn, world.layout, world.config, world.issue_id, ["P-0001"], ["x"], {})
    assert found.manifest["checks"][0]["targets"] == [SERVICE_PATH]
    assert "rule: demo.unchecked-id" in found.files["static-1.yaml"]


def api_files():
    check = {"id": "api-1", "kind": "api", "role": "Admin", "file": "api-1.request.json", "location": "GET /a",
             "requires": ["backend"], "precondition": None}
    return repro.ReproFiles({"issue": "0001", "problems": ["a1b2c3d4e5f60718"], "checks": [check]},
                            {"api-1.request.json": json.dumps(REQUEST),
                             "api-1.expect.json": json.dumps({"status": {"notIn": ["5xx"]}})})


TEST_FILE = "tests/test_order.py"
TEST_CODE = "def test_owner():\n    assert get_order(42).company == 7\n"
COMMANDS = (CheckCommand("pytest", ".", "python -m pytest -q"),)


def written(**changes):
    output = {"analysis": "x", "status": "written", "file": TEST_FILE, "command": f"python -m pytest -q {TEST_FILE}",
              "location": f"{SERVICE_PATH}:12", "covers": ["只返回本公司的订单"], "reason": None}
    output.update(changes)
    return output


def rules(worktree, changed, test_paths=("tests/",), untracked=None):
    new = changed if untracked is None else untracked
    return repro_test.TestRules(worktree, test_paths, COMMANDS, lambda root: changed, lambda root: new, None,
                                worktree, worktree)


def test_repro_tests_may_only_touch_test_files_and_use_an_allowed_command(tmp_path):
    assert repro_test.problems(written(), rules(tmp_path, [TEST_FILE])) == []
    assert repro_test.problems(written(), rules(tmp_path, [TEST_FILE], ())) == [
        "项目没有配置 testPaths，不能写复现测试"]
    found = repro_test.problems(written(), rules(tmp_path, [TEST_FILE, SERVICE_PATH]))
    assert found == [f"只能改动测试文件，以下文件不在 testPaths 中：{SERVICE_PATH}"]
    assert repro_test.problems(written(), rules(tmp_path, [])) == [f"测试文件 {TEST_FILE} 没有新建"]
    assert repro_test.problems(written(), rules(tmp_path, [TEST_FILE], untracked=[])) == [
        f"改动了已有的测试文件 {TEST_FILE}：复现测试须新建独立的文件，已有测试恢复原样"]
    other = "tests/test_other.py"
    assert repro_test.problems(written(), rules(tmp_path, [TEST_FILE, other], untracked=[TEST_FILE])) == [
        f"改动了已有的测试文件 {other}：复现测试须新建独立的文件，已有测试恢复原样"]
    [problem] = repro_test.problems(written(command=f"python -c x {TEST_FILE}"), rules(tmp_path, [TEST_FILE]))
    assert "允许前缀为 python -m pytest -q" in problem


def test_a_registered_repro_test_joins_the_deterministic_checks_and_is_placed(tmp_path):
    world = make_fix_world(tmp_path)
    directory = world.layout.regression_dir("0001")
    repro.write(world.conn, directory, "regressions/0001/check.yaml", api_files())
    worktree = tmp_path / "wt"
    (worktree / "tests").mkdir(parents=True)
    (worktree / TEST_FILE).write_text(TEST_CODE, encoding="utf-8")
    test = repro_test.ReproTest(repro_test.PASSED, output=written())
    check = repro_test.register(world.conn, directory, "regressions/0001/check.yaml", "0001", ["a1b2c3d4e5f60718"],
                                test, worktree)
    assert check.kind is RegressionKind.TEST and (directory / "test-1.py").read_text(encoding="utf-8") == TEST_CODE
    assert [entry.id for entry in manifest.load(directory).checks] == ["api-1", "test-1"]
    assert [item.check_id for item in regressions.find(world.conn, issue_id="0001")] == ["api-1", "test-1"]
    assert manifest.placed(directory, worktree) == {TEST_FILE}
    (worktree / TEST_FILE).write_text("def test_owner():\n    pass\n", encoding="utf-8")
    assert manifest.placed(directory, worktree) == set() and manifest.place_tests(directory, worktree) == [TEST_FILE]
    manual = repro_test.register(None, tmp_path / "manual", "regressions/0002/check.yaml", "0002", [], test,
                                 tmp_path / "wt")
    assert manual.check_id == "test-1"
    signed = repro_test.ReproTest(repro_test.PASSED, output=written(expectedSignature="KeyError: 'group_key'"))
    repro_test.register(None, tmp_path / "signed", "regressions/0003/check.yaml", "0003", [], signed, worktree)
    assert manifest.load(tmp_path / "signed").checks[0].expected_signature == "KeyError: 'group_key'"


def test_written_checks_are_registered_with_their_hash(tmp_path):
    world = make_fix_world(tmp_path)
    directory = world.layout.regression_dir("0001")
    checks = repro.write(world.conn, directory, "regressions/0001/check.yaml", api_files())
    [stored] = regressions.find(world.conn, issue_id="0001")
    assert stored == checks[0] and stored.kind is RegressionKind.API and stored.requires == ("backend",)
    entry = manifest.load(directory).entry("api-1")
    assert stored.hash == manifest.entry_hash(directory, entry)
    assert repro.existing(world.conn, "0001") == checks
