from datetime import timedelta

from fix_world import SERVICE_PATH
from release_world import BRANCH, NOW, Planner, to_submit

from tightrein.domain.clock import FixedClock
from tightrein.domain.enums import (
    IssuePhase,
    CloseReason,
    DeploymentStatus,
    HandoffStatus,
    IssueEvent,
    IssueStatus,
    OperationKind,
    ProblemStatus,
    RunStage,
)
from tightrein.pipeline.common import stage_runs
from tightrein.pipeline.release.service import ReleaseDeps, ReleaseService
from tightrein.pipeline.release.steps import work_summary
from tightrein.store.repos import deployments, issues, problems, pulls
from tightrein.store.repos.pulls import PullRecord
from tightrein.vcs.errors import NetworkError
from tightrein.pipeline.common.deploys import DeployRecord, Found
from tightrein.vcs.parse import PullState

MERGE = "9" * 40
URL = "https://example.test/pull/187"


class Gh:
    def __init__(self, pull=None, deployment=None, fail=False, configured=True):
        self.pull = pull or PullState(187, URL, "OPEN")
        self.deployment = deployment or Found(DeploymentStatus.PENDING)
        self.fail = fail
        self.deploys = Deploys(self.deployment, configured)

    def pr_view(self, repo, ref):
        if self.fail:
            raise NetworkError("网络不可用")
        return self.pull


class Deploys:
    """部署来源(pipeline/common/deploys.DeploySource)的替身。"""

    def __init__(self, found, configured=True):
        self.found = found
        self._configured = configured

    def configured(self):
        return self._configured

    def find(self, commit):
        return self.found


class Notifier:
    def __init__(self):
        self.sent = []

    def notify(self, event, subject, text):
        self.sent.append((event, subject))


def reviewing(tmp_path, **config):
    world, git = to_submit(tmp_path, **config)
    world.event(IssueEvent.PR_CREATED, actor="release", updates={"pr": URL})
    pulls.save(world.conn, PullRecord(world.issue_id, 187, URL, BRANCH, "Fix order 500", "OPEN", NOW))
    return world, git


def releaser(world, git, gh, notifier=None, planner=None):
    return ReleaseService(ReleaseDeps(world.layout, world.config, world.conn, world.clock, world.events, git,
                                      planner or Planner(), gh, notifier, deploys=gh.deploys))


def history(world):
    return (world.layout.root / issues.get(world.conn, world.issue_id).path).read_text(encoding="utf-8")


def run(sha=MERGE, status="succeeded"):
    return DeployRecord("31", sha, status, "staging", "https://ci.example.test/runs/31", NOW)


def test_merged_prs_move_on_to_deployment_tracking(tmp_path):
    world, git = reviewing(tmp_path)
    gh = Gh(PullState(187, URL, "MERGED", merge_commit=MERGE, merged_at=NOW),
            Found(DeploymentStatus.SUCCEEDED, run()))
    report = releaser(world, git, gh).track()
    assert world.issue().phase is IssuePhase.DEPLOY_CHECK
    assert pulls.get(world.conn, world.issue_id).merge_commit == MERGE
    assert deployments.get(world.conn, MERGE).status is DeploymentStatus.SUCCEEDED
    assert any(line.startswith(f"Issue {world.issue_id} 已部署(部署 commit 999999999999)，等待部署后确认")
               for line in report.lines)
    assert stage_runs.latest_outputs(world.conn, world.layout, RunStage.RELEASE, world.issue_id)[1][
        "deployments"][-1]["source"] == "deploy-source"
    assert releaser(world, git, gh).track().lines == []


def test_without_a_deploy_source_the_merge_time_plus_observation_counts_as_deployed(tmp_path):
    world, git = reviewing(tmp_path, target={"baseUrl": "https://staging.example.test"})
    gh = Gh(PullState(187, URL, "MERGED", merge_commit=MERGE, merged_at=NOW), configured=False)
    lines = releaser(world, git, gh).track().lines
    assert world.issue().phase is IssuePhase.DEPLOY_CHECK
    assert any("以合并时间加观察期为准：2026-10-06T03:00:00Z 之后进行部署后确认" in line for line in lines)
    assert deployments.get(world.conn, MERGE) is None
    world.clock = FixedClock(NOW + timedelta(hours=25))
    lines = releaser(world, git, gh).track().lines
    assert any("合并后已过观察期，视为已部署" in line for line in lines)
    found = deployments.get(world.conn, MERGE)
    assert found.status is DeploymentStatus.SUCCEEDED and found.deployed_at == NOW + timedelta(hours=24)
    outputs = stage_runs.latest_outputs(world.conn, world.layout, RunStage.RELEASE, world.issue_id)[1]
    assert outputs["deployments"][-1]["source"] == "merge-time"


def test_closed_prs_reject_the_fix_and_keep_the_user_note(tmp_path):
    world, git = reviewing(tmp_path)
    gh = Gh(PullState(187, URL, "CLOSED", closed_at=NOW, comments=({"body": "这不是缺陷，是配置问题"},)))
    report = releaser(world, git, gh).track()
    issue = world.issue()
    assert (issue.status, issue.close_reason) == (IssueStatus.CANCELLED, CloseReason.FIX_REJECTED)
    assert problems.get(world.conn, "P-0001").status is ProblemStatus.IGNORED
    assert "用户说明：这不是缺陷，是配置问题" in history(world)
    assert "retriage --verdict" in report.lines[0]


def test_open_prs_are_reminded_once_a_day_and_conflicts_notify(tmp_path):
    world, git = reviewing(tmp_path)
    world.clock.advance(timedelta(days=7))
    notifier = Notifier()
    gh = Gh(PullState(187, URL, "OPEN", mergeable="CONFLICTING"))
    lines = releaser(world, git, gh, notifier).track().lines
    assert any("超过提醒期限" in line for line in lines) and any("release sync" in line for line in lines)
    assert notifier.sent == [("release-pr-conflict", world.issue_id)]
    assert not any("超过提醒期限" in line for line in releaser(world, git, gh, notifier).track().lines)


def test_failed_deployments_and_manual_parts_are_reported(tmp_path):
    world, git = reviewing(tmp_path, target={"baseUrl": "https://staging.example.test", "healthcheck": "/health",
                                             "manualDeployPaths": ["src/Services/"]})
    notifier = Notifier()
    merged = PullState(187, URL, "MERGED", merge_commit=MERGE, merged_at=NOW)
    lines = releaser(world, git, Gh(merged, Found(DeploymentStatus.FAILED, run(status="failed"))),
                     notifier).track().lines
    assert "部署失败" in lines[-1] and ("release-deploy-failed", world.issue_id) in notifier.sent
    later = run(sha="8" * 40)
    lines = releaser(world, git, Gh(merged, Found(DeploymentStatus.SUCCEEDED, later)), notifier).track().lines
    assert any(SERVICE_PATH in line and "手动部署" in line for line in lines)
    outputs = stage_runs.latest_outputs(world.conn, world.layout, RunStage.RELEASE, world.issue_id)[1]
    assert outputs["deployments"][-1]["commit"] == "8" * 40


def test_master_dates_are_recorded_and_query_failures_skip(tmp_path):
    world, git = reviewing(tmp_path)
    git.branches_containing = lambda repo, commit, remote=True: ["origin/main", "origin/master"]
    merged = PullState(187, URL, "MERGED", merge_commit=MERGE, merged_at=NOW)
    lines = releaser(world, git, Gh(merged)).track().lines
    assert f"Issue {world.issue_id} 的修复已进入 origin/master" in lines
    assert pulls.get(world.conn, world.issue_id).master_at == NOW
    world2, git2 = reviewing(tmp_path / "offline")
    report = releaser(world2, git2, Gh(fail=True)).track()
    assert report.skipped == [(world2.issue_id, "只读查询失败：网络不可用")] and "跳过" in report.summary


def test_cleanup_needs_two_confirmations_and_a_clean_worktree(tmp_path):
    world, git = reviewing(tmp_path)
    world.event(IssueEvent.PR_MERGED, actor="release")
    world.event(IssueEvent.STAGING_VERIFIED, actor="verify")
    planner = Planner()

    def plan_cleanup(issue_id, worktree, branch):
        return planner._operation(OperationKind.CLEANUP, issue_id)

    planner.plan_cleanup = plan_cleanup
    service = releaser(world, git, Gh(), planner=planner)
    blocked = service.cleanup(world.issue_id)
    assert blocked.status is HandoffStatus.BLOCKED and SERVICE_PATH in blocked.message
    git.base = {path: (world.worktree / path).read_text() for path in git.base}
    result = service.cleanup(world.issue_id)
    assert result.operation is not None and "第二次确认时请输入分支名" in result.message
    assert "git branch -d 只删除已合并的分支" in result.message


def test_work_summary_and_pr_comment(tmp_path):
    world, git = reviewing(tmp_path)
    service = releaser(world, git, Gh())
    assert service.summary(world.issue_id).status is HandoffStatus.BLOCKED
    world.event(IssueEvent.PR_MERGED, actor="release")
    result = service.summary(world.issue_id)
    text = result.path.read_text(encoding="utf-8")
    assert text.startswith("# 订单查询返回 500") and "- 合并前验证：通过" in text and "无界面变化" in text
    assert len([line for line in text.splitlines() if line.startswith("- ")]) <= 4
    assert service.comment(world.issue_id).status is HandoffStatus.BLOCKED


def test_work_summary_does_not_claim_no_ui_change_when_pages_were_not_verified():
    patrol = {"id": "page-checks", "category": "page-patrol", "command": None, "result": "unverified",
              "evidence": [], "reason": "page 模式的端口被占用，页面类检查未验证；空出端口后重跑"}
    local = {"items": [patrol]}
    assert work_summary.page_note(local) == ("页面验证未执行(page 模式的端口被占用，页面类检查未验证；空出端口后重跑)，"
                                             "界面变化未确认")
    assert work_summary.page_note(None) == "页面验证未执行(没有合并前验证的结果)，界面变化未确认"
    assert work_summary.page_note({"items": [dict(patrol, result="pass")]}) is None
    text = work_summary.summary("标题", "结论", {}, [], [], 4, 2, work_summary.page_note(local))
    assert "界面变化未确认" in text and work_summary.NO_UI not in text
