import json
from dataclasses import replace
from datetime import timedelta

from pipeline_world import NOW, PROJECT
from verify_world import fix_handoff, fixed_world, to_verify

from tightrein.domain.enums import (
    CloseReason,
    DeploymentStatus,
    HandoffStatus,
    IssueEvent,
    IssuePhase,
    IssueStatus,
    Probe,
    RegressionKind,
    RegressionResult,
    RunStage,
    Stage,
)
from tightrein.evaluation import run_cases
from tightrein.evaluation.cases import run_case
from tightrein.pipeline.checks.regressions.runner import RegressionOutcome
from tightrein.pipeline.common import stage_runs
from tightrein.pipeline.verify.service import VerifyDeps, VerifyService
from tightrein.sources.common.procs import ToolRun
from tightrein.store.files.layout import ToolLayout
from tightrein.store.repos import deployments, issue_events, problems, pulls, regressions
from tightrein.store.repos.deployments import Deployment
from tightrein.store.repos.pulls import PullRecord

MERGE = "9" * 40
DEPLOYED = "8" * 40


class Executor:
    def __init__(self, result=RegressionResult.PASSED):
        self.result = result
        self.targets = []

    def __call__(self, target):
        self.targets.append(target)
        return self

    def run_checks(self, checks, target):
        return [RegressionOutcome(check, self.result, "GET /api/Order/{id}", "detail") for check in checks]


class Git:
    def __init__(self, base):
        self.base = base

    def __getattr__(self, name):
        return getattr(self.base, name)

    def is_ancestor(self, repo, commit, of):
        return True


class Revert:
    def __init__(self):
        self.calls = []

    def __call__(self, issue_id, reason):
        self.calls.append((issue_id, reason))
        return type("Result", (), {"operation": "OP-0009", "message": "撤销 PR 待确认"})()


def merged(tmp_path, deployed=True, **config):
    world = fixed_world(tmp_path, **config)
    git = world.git()
    fix = to_verify(world, git)
    fix_handoff(world, git, changedFiles=[{"path": "src/Services/OrderService.src", "added": 1, "removed": 1}],
                diffHash=fix["diffHash"])
    for event in (IssueEvent.VERIFY_PASSED, IssueEvent.PR_CREATED, IssueEvent.PR_MERGED):
        world.event(event, actor="release")
    pulls.save(world.conn, PullRecord(world.issue_id, 187, "https://example.test/pull/187", "cty/fix-order-500",
                                      "Fix order 500", "MERGED", NOW, merge_commit=MERGE))
    if deployed:
        deployments.save(world.conn, Deployment(DEPLOYED, DeploymentStatus.SUCCEEDED, NOW, "31", deployed_at=NOW))
    return world, Git(git)


def verifier(world, git, executor=None, synced=None, revert=None):
    return VerifyService(VerifyDeps(world.layout, ToolLayout(), world.config, world.conn, world.clock, world.events,
                                    world.runner, git, lambda command: ToolRun(0), executor=executor or Executor(),
                                    sync=(synced.append if synced is not None else None), revert=revert))


def test_a_replayed_check_that_still_fails_reverts_and_reopens(tmp_path):
    world, git = merged(tmp_path)
    executor = Executor(RegressionResult.FAILED)
    revert = Revert()
    result = verifier(world, git, executor, revert=revert).staging(world.issue_id)
    assert result.conclusion == "failed" and world.issue().status is IssueStatus.TODO
    assert executor.targets[0].base_url == "https://staging.example.test" and executor.targets[0].worktree is None
    assert len(revert.calls) == 1 and "回归" in revert.calls[0][1]
    outputs = result.handoff.read_text(encoding="utf-8")
    assert "OP-0009" in outputs and '"method": "replay"' in outputs


def test_a_passing_replay_confirms_the_fix(tmp_path):
    world, git = merged(tmp_path)
    result = verifier(world, git).staging(world.issue_id)
    issue = world.issue()
    assert result.conclusion == "passed"
    assert (issue.status, issue.close_reason, issue.phase) == (IssueStatus.DONE, CloseReason.FIXED, None)


def test_unrunnable_checks_fall_back_to_the_observation_period(tmp_path):
    world, git = merged(tmp_path)
    result = verifier(world, git, Executor(RegressionResult.NOT_RUN)).staging(world.issue_id)
    assert result.status is HandoffStatus.BLOCKED and "观察期到" in result.message
    assert world.issue().phase is IssuePhase.DEPLOY_CHECK
    world.clock.advance(timedelta(hours=49))
    assert verifier(world, git, Executor(RegressionResult.NOT_RUN)).staging(world.issue_id).conclusion == "passed"


def test_platform_problems_regress_when_seen_after_the_deployment(tmp_path):
    world, git = merged(tmp_path)
    problem = problems.get(world.conn, "P-0001")
    problems.save(world.conn, replace(problem, probe=Probe.PLATFORM_ERRORS, last_seen_at=NOW + timedelta(hours=2)))
    regressions.save(world.conn, replace(regressions.get(world.conn, world.issue_id, "api-1"),
                                         kind=RegressionKind.STATIC))
    synced = []
    result = verifier(world, git, synced=synced).staging(world.issue_id)
    assert result.conclusion == "failed" and synced == [DEPLOYED] and world.issue().status is IssueStatus.TODO


def test_missing_deployments_block(tmp_path):
    world, git = merged(tmp_path)
    deployments.save(world.conn, Deployment(DEPLOYED, DeploymentStatus.FAILED, NOW, "31"))
    assert verifier(world, git).staging(world.issue_id).message.startswith("还没有包含合并提交的成功部署")


def test_without_a_deploy_source_staging_waits_for_the_observation_period(tmp_path):
    world, git = merged(tmp_path, deployed=False, target={"baseUrl": "https://staging.example.test"})
    result = verifier(world, git).staging(world.issue_id)
    assert result.status is HandoffStatus.BLOCKED and "观察期" in result.message
    assert world.issue().phase is IssuePhase.DEPLOY_CHECK


def test_the_target_environment_follows_the_config(tmp_path):
    world, git = merged(tmp_path, target={**PROJECT["target"], "environment": "production"})
    executor = Executor()
    verifier(world, git, executor).staging(world.issue_id)
    assert executor.targets[0].environment == "production"


class PatchGit(Git):
    def patch(self, repo, base, head=None):
        return f"diff {base[:4]}..{head[:4]}\n"


def test_a_confirmed_fix_is_saved_as_an_evaluation_case(tmp_path):
    world, git = merged(tmp_path)
    issue_run = stage_runs.begin(RunStage.ISSUE, world.layout, world.conn, world.clock, world.events)
    issue_run.handoff(RunStage.ISSUE, world.issue_id, HandoffStatus.OK, {
        "issueId": world.issue_id, "action": "created", "path": "issues/x.md", "problems": ["P-0001"],
        "severity": "P1", "treatment": None, "labels": [], "acceptance": ["返回 404"], "notified": False}, "放行")
    assert verifier(world, PatchGit(git.base)).staging(world.issue_id).conclusion == "passed"
    saved = json.loads(world.layout.run_case(world.issue_id).read_text(encoding="utf-8"))
    assert (saved["issueId"], saved["baseCommit"], saved["mergeCommit"], saved["deployCommit"]) == (
        world.issue_id, "1" * 40, MERGE, DEPLOYED)
    assert saved["patch"] == "diff 1111..9999\n" and saved["input"] == f"{world.issue_id}.input.json"
    assert saved["reproduceTests"][0]["kind"] == "api" and saved["reproduceTests"][0]["content"]
    assert saved["issue"].startswith("---")
    (data,), hashes, problems = run_cases.read(world.layout)
    case = run_case(world.layout, data)
    assert problems == [] and list(hashes) == [f"fix/{world.issue_id}"] and case.module is Stage.FIX
    assert (case.id, case.commit, case.source["subjectId"]) == (world.issue_id, "1" * 40, world.issue_id)
    assert case.input_file == world.layout.run_case(world.issue_id, ".input.json") and case.input_file.is_file()


def test_a_confirmed_fix_without_material_is_not_saved(tmp_path):
    world, git = merged(tmp_path)
    assert verifier(world, git).staging(world.issue_id).conclusion == "passed"
    assert not world.layout.run_case(world.issue_id).exists()
    notes = [record.note for record in issue_events.TABLE.find(world.conn, event=IssueEvent.STAGING_VERIFIED.value)]
    assert notes[-1].endswith("没有保存评测用例：没有 Issue 文件或 issue 环节的交接文档")
