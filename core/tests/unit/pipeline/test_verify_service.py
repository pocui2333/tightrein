from dataclasses import replace
from types import SimpleNamespace

from fix_world import make_fix_world
from verify_world import (
    PORTS,
    Client,
    Probe,
    Spawner,
    backend,
    fix_handoff,
    fixed_world,
    local_factory,
    service,
    to_verify,
)

from tightrein.domain.enums import (
    CheckResult,
    IssuePhase,
    HandoffStatus,
    IssueEvent,
    IssueStatus,
    OperationKind,
    RegressionResult,
    RunStage,
    RunStatus,
    Stage,
    VerifyPhase,
)
from tightrein.pipeline.common import stage_runs
from tightrein.pipeline.fix.steps import repro
from tightrein.pipeline.checks.pages.runner import PageRun
from tightrein.pipeline.verify.service import PAGES, VerifyDeps, VerifyService
from tightrein.sources.base import ProbeOutcome
from tightrein.sources.common.procs import ToolRun
from tightrein.pipeline.checks.regressions.runner import RegressionOutcome
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout
from tightrein.store.repos import pending_operations, regressions, scores
from tightrein.vcs.executor import OperationRunner


class Executor:
    def __init__(self, results=None):
        self.results = dict(results or {})
        self.targets = []

    def __call__(self, target):
        self.targets.append(target)
        return self

    def run_checks(self, checks, target):
        found = []
        for check in checks:
            result, met = self.results.get((check.issue_id, check.check_id), (RegressionResult.PASSED, True))
            found.append(RegressionOutcome(check, result, "GET /api/Order/{id}", "状态码 200", met))
        return found


class Launcher:
    def __init__(self, code=0):
        self.code = code
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        return ToolRun(self.code)


class FakeProbe:
    def __init__(self, outcome=None):
        self.outcome = outcome or ProbeOutcome(RunStatus.OK)
        self.calls = []

    def run(self, target, level, options):
        self.calls.append(options)
        return self.outcome


class FakePages:
    """页面运行器的替身：巡检时返回预设的结果。"""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def run(self, target, **options):
        self.calls.append(options)
        return self.outcome


def verifier(world, git, *, executor=None, client=None, spawner=None, probe=None, probes=None, layout=None,
             launcher=None, page_checks=False):
    local = None
    if client is not None:
        local = local_factory(world, client, spawner or Spawner({"backend": "listening\n", "frontend": "listening\n"}),
                              probe)
    return VerifyService(VerifyDeps(
        layout or world.layout, ToolLayout(), world.config, world.conn, world.clock, world.events, world.runner, git,
        launcher or Launcher(), executor=executor or Executor(), local=local,
        probes=(lambda target: probes) if probes is not None else None,
        operations=OperationRunner(world.conn, None, None, None, world.layout, "R-20261005-030000-verify"),
        page_checks=page_checks))


def latest(world, phase=VerifyPhase.LOCAL):
    return stage_runs.latest_outputs(world.conn, world.layout, RunStage.VERIFY, world.issue_id, phase)[1]


def test_local_checks_run_against_local_services_when_endpoints_change(tmp_path):
    world = fixed_world(tmp_path, checks={"commands": [{"name": "unit", "cwd": ".", "command": "make test"}]})
    git = world.git()
    to_verify(world, git)
    other = repro.ReproFiles({"issue": "0003", "problems": ["a1b2c3d4e5f60718"], "checks": [
        {"id": "api-1", "kind": "api", "role": "Admin", "file": "api-1.request.json", "location": "GET /api/Order/{id}",
         "requires": ["backend"], "precondition": None}]}, {
        "api-1.request.json": '{"method": "GET", "path": "/x", "pathTemplate": "/x"}',
        "api-1.expect.json": '{"status": {"notIn": ["5xx"]}}'})
    [related] = repro.write(world.conn, world.layout.regression_dir("0003"), "regressions/0003/check.yaml", other)
    regressions.save(world.conn, replace(related, last_result=RegressionResult.PASSED))
    launcher = Launcher()
    shallow = FakeProbe()
    result = verifier(world, git, client=Client([backend()]), probes={"api-fuzz": shallow},
                      launcher=launcher).local(world.issue_id)
    assert (result.status, result.conclusion) == (HandoffStatus.OK, "passed"), result.message
    outputs = latest(world)
    assert [item["id"] for item in outputs["items"]] == ["repro:api-1", "other:0003/api-1", "api-shallow"]
    assert shallow.calls[0].include_paths == ("/api/Order/{id}",)
    assert world.issue().phase is IssuePhase.SUBMIT
    assert scores.find(world.conn, stage=Stage.FIX) == [] and launcher.commands == []
    assert regressions.get(world.conn, world.issue_id, "api-1").last_result is RegressionResult.PASSED


def test_other_regressions_come_from_the_fix_and_are_not_rerun(tmp_path):
    world = fixed_world(tmp_path)
    git = world.git()
    to_verify(world, git, affectedEndpoints=[], otherRegressions=[
        {"issueId": "0004", "checkId": "static-1", "kind": "static", "result": "failed"}])
    executor = Executor()
    result = verifier(world, git, executor=executor, client=Client([backend()])).local(world.issue_id)
    outputs = latest(world)
    assert result.conclusion == "failed" and [item["id"] for item in outputs["items"]] == ["other:0004/static-1"]
    assert executor.targets == [] and outputs["skipped"] == ["改动不涉及接口；改动不涉及前端，或没有配置页面检查"]


def test_without_local_run_no_service_is_started(tmp_path):
    world = fixed_world(tmp_path)
    git = world.git()
    to_verify(world, git)
    result = verifier(world, git).local(world.issue_id)
    outputs = latest(world)
    assert result.conclusion == "passed" and outputs["items"] == []
    assert outputs["skipped"] == ["没有配置本机启动(local-run)，不启动本机服务"]


def test_failures_go_back_to_the_fix_and_hold_after_the_limit(tmp_path):
    world = fixed_world(tmp_path)
    git = world.git()
    to_verify(world, git)
    failing = Executor({(world.issue_id, "api-1"): (RegressionResult.FAILED, True)})
    result = verifier(world, git, executor=failing, client=Client([backend()])).local(world.issue_id)
    assert result.conclusion == "failed" and world.issue().status is IssueStatus.TODO and world.issue().hold is None
    world.event(IssueEvent.FIX_STARTED, actor="fix")
    fix_handoff(world, git)
    world.event(IssueEvent.FIX_DONE, actor="fix")
    verifier(world, git, executor=failing, client=Client([backend()])).local(world.issue_id)
    assert latest(world)["consecutiveFailures"] == 2
    assert world.issue().hold.reason == "合并前验证连续 2 次失败"


def test_missing_prerequisites_and_services(tmp_path):
    world = fixed_world(tmp_path)
    git = world.git()
    assert verifier(world, git).local(world.issue_id).message == f"先执行 fix done {world.issue_id}"
    world = fixed_world(tmp_path / "unverified")
    git = world.git()
    to_verify(world, git)
    result = verifier(world, git, client=Client([backend()], [{"name": "compute", "reason": "外部节点"}]),
                      executor=Executor()).local(world.issue_id)
    assert result.conclusion == "passed"


def test_migrations_wait_for_a_confirmation(tmp_path):
    world = fixed_world(tmp_path)
    git = world.git()
    (world.worktree / "db").mkdir()
    (world.worktree / "db" / "0042.sql").write_text("ALTER TABLE orders ADD company\n", encoding="utf-8")
    to_verify(world, git, migration={"entries": ["0042"], "reversible": True, "revertMethod": "回滚 0042"})
    client = Client([backend()], migration_paths=["db/*"])
    spawner = Spawner({"backend": "listening\n"})
    result = verifier(world, git, client=client, spawner=spawner).local(world.issue_id)
    assert (result.status, result.conclusion) == (HandoffStatus.BLOCKED, "awaiting-user")
    assert "--confirm-migration" in result.message and spawner.started == []
    operation = pending_operations.get(world.conn, latest(world)["migration"]["operationId"])
    assert operation.kind is OperationKind.LOCAL_MIGRATION and world.issue().phase is IssuePhase.VERIFY
    result = verifier(world, git, client=client, spawner=spawner).local(world.issue_id, confirm_migration=True)
    assert result.conclusion == "passed" and latest(world)["migration"]["operationId"] == operation.id


def test_screenshots_are_reviewed_or_left_to_the_user(tmp_path):
    world = fixed_world(tmp_path)
    git = world.git()
    to_verify(world, git, affectedPages=["/orders"])
    client = Client([service("backend", 5102), service("frontend", 5103)])
    patrol = FakePages(PageRun(RunStatus.OK, artifacts=("orders.png",)))
    world.runner.add("fix-reviewer-screenshot", {"mode": "screenshot", "items": [], "blockers": [], "unverified": [],
                                                 "screenshots": [{"path": "raw/orders.png", "result": "unknown",
                                                                  "reason": "表格被遮挡还是空数据看不清"}]})
    service_ = verifier(world, git, client=client, probes={"api-fuzz": FakeProbe(), PAGES: patrol},
                        page_checks=True)
    result = service_.local(world.issue_id)
    assert (result.status, result.conclusion) == (HandoffStatus.BLOCKED, "awaiting-user")
    task = world.runner.tasks[-1]
    assert task.read_paths and task.read_paths[0].endswith("raw/orders.png") and client.calls[0][0] == "page"
    result = service_.screenshots(world.issue_id, ok=False, note="操作列盖住了内容")
    assert result.conclusion == "failed" and world.issue().status is IssueStatus.TODO


def test_occupied_page_ports_fall_back_to_api_mode(tmp_path):
    world = fixed_world(tmp_path)
    git = world.git()
    to_verify(world, git, affectedPages=["/orders"])
    client = Client([service("backend", 5102), service("frontend", 5103)])
    result = verifier(world, git, client=client, probe=Probe({5103}), page_checks=True).local(world.issue_id)
    assert [call[0] for call in client.calls] == ["page", "api"]
    items = {item["id"]: item for item in latest(world)["items"]}
    assert items["page-checks"]["result"] == "unverified" and result.conclusion == "passed"


def test_output_mode_leaves_the_issue_alone(tmp_path):
    world = fixed_world(tmp_path)
    git = world.git()
    to_verify(world, git)
    layout = WorkspaceLayout(world.layout.root, tmp_path / "out")
    result = verifier(world, git, client=Client([backend()]), layout=layout).local(world.issue_id)
    assert result.conclusion == "passed" and world.issue().phase is IssuePhase.VERIFY
    assert (tmp_path / "out" / "handoff" / f"verify-local-{world.issue_id}.json").is_file()
    assert scores.find(world.conn, stage=Stage.FIX) == []


def test_a_manual_issue_is_verified_without_reproduction(tmp_path):
    world = make_fix_world(tmp_path, manual=("按日期筛选订单", "订单列表按日期筛选。\n"), localRun=PORTS,
                           checks={"commands": [{"name": "unit", "cwd": ".", "command": "make test"}]})
    world.event(IssueEvent.FIX_STARTED, actor="fix")
    git = world.git()
    to_verify(world, git)
    result = verifier(world, git, client=Client([backend()])).local(world.issue_id)
    assert (result.status, result.conclusion) == (HandoffStatus.OK, "passed"), result.message
    assert [item["id"] for item in latest(world)["items"]] == ["api-shallow"]
    assert world.issue().phase is IssuePhase.SUBMIT
    assert scores.find(world.conn, stage=Stage.FIX) == []


def test_projects_without_local_run_ports_have_no_service_url(tmp_path):
    world = make_fix_world(tmp_path)
    assert verifier(world, world.git())._local_url("api") == "localhost"


def test_page_patrol_fails_only_on_failures_of_the_affected_pages(tmp_path):
    from tightrein.pipeline.checks.pages.failures import PageFailure
    from tightrein.pipeline.verify.steps import regression

    target = SimpleNamespace(raw_dir=tmp_path)
    failures = (PageFailure("/orders/7", "console-error", "TypeError: x", "订单"),
                PageFailure("/users", "case-failure", "找不到按钮", "用户"))
    item, shots = regression.patrol(FakePages(PageRun(RunStatus.OK, failures, ("a.png", "log.txt"))), target,
                                    ["/orders/:id"])
    assert item.result is CheckResult.FAIL and "/orders/7 TypeError: x" in item.reason and "/users" not in item.reason
    assert [shot.path for shot in shots] == ["a.png"]
    passed, _ = regression.patrol(FakePages(PageRun(RunStatus.OK, failures[1:])), target, ["/orders/:id"])
    assert passed.result is CheckResult.PASS
    skipped, _ = regression.patrol(FakePages(PageRun(RunStatus.SKIPPED, notes=("没有目标地址",))), target, ["/a"])
    assert skipped.result is CheckResult.UNVERIFIED and skipped.reason == "没有目标地址"
    missing, _ = regression.patrol(None, target, ["/a"])
    assert missing.result is CheckResult.UNVERIFIED
