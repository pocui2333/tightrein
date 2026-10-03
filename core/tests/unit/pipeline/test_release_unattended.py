from dataclasses import replace

from release_world import BRANCH, HEAD, NOW, Planner, to_submit

from tightrein.domain.enums import (
    IssuePhase,
    HandoffStatus,
    IssueEvent,
    IssueStatus,
    OperationKind,
    OperationStatus,
    RunStage,
)
from tightrein.pipeline.common import stage_runs
from tightrein.pipeline.release.service import ReleaseDeps, ReleaseService
from tightrein.pipeline.release.steps import auto_merge
from tightrein.store.repos import issues, pending_operations, pulls
from tightrein.store.repos.pulls import PullRecord
from tightrein.vcs.executor import OperationResult
from tightrein.vcs.parse import Commit, MergeFacts, PullState

URL = "https://example.test/pull/187"
DIRECT = {"gates": {"release-writes": "auto"}}


class DirectPlanner(Planner):
    def __init__(self):
        super().__init__()
        self.made = {}

    def _operation(self, kind, issue_id, files=(), text="操作", result=None, extra=None):
        operation = super()._operation(kind, issue_id, files, text, result, extra)
        self.made[operation.id] = operation
        return operation

    def plan_merge_pull_request(self, issue_id, number, slug, branch, head, method, auto=False):
        self.merge = (number, slug, branch, head, method) + (("auto",) if auto else ())
        return self._operation(OperationKind.MERGE_PULL_REQUEST, issue_id, result={"outputs": [""]})


class Operations:
    """run_unattended 的替身：按操作类型模拟仓库变化后调用发布的 follow_up。"""

    def __init__(self, planner, git, gh=None, status=OperationStatus.EXECUTED):
        self.planner = planner
        self.git = git
        self.gh = gh
        self.status = status
        self.ran = []
        self.service = None

    def run_unattended(self, operation_id, *, reason, clock):
        operation = self.planner.made[operation_id]
        self.ran.append((operation.kind, reason))
        if self.status is not OperationStatus.EXECUTED:
            return OperationResult(replace(operation, status=self.status, result={"error": "PushRejected"}), ran=True)
        if operation.kind is OperationKind.COMMIT:
            self.git.base = self.git._current()
        if operation.kind is OperationKind.PUSH:
            self.git.pushed = True
        if operation.kind is OperationKind.MERGE_PULL_REQUEST and "auto" not in self.planner.merge:
            self.gh.pull = PullState(187, URL, "MERGED", merge_commit="9" * 40, merged_at=NOW)
        self.service.on_executed(operation)
        return OperationResult(operation, ran=True)


def releaser(world, git, planner, operations, gh=None):
    service = ReleaseService(ReleaseDeps(world.layout, world.config, world.conn, world.clock, world.events, git,
                                         planner, gh, operations=operations))
    operations.service = service
    return service


def outputs(world):
    return stage_runs.latest_outputs(world.conn, world.layout, RunStage.RELEASE, world.issue_id)[1]


def history(world):
    return (world.layout.root / issues.get(world.conn, world.issue_id).path).read_text(encoding="utf-8")


def test_release_runs_commit_push_and_pr_directly(tmp_path):
    world, git = to_submit(tmp_path, **DIRECT)
    planner = DirectPlanner()
    operations = Operations(planner, git)
    result = releaser(world, git, planner, operations).release(world.issue_id)
    assert [kind for kind, _ in operations.ran] == [OperationKind.COMMIT, OperationKind.PUSH,
                                                    OperationKind.PULL_REQUEST, OperationKind.PR_COMMENT]
    assert planner.comment.startswith("以下是 tightrein 的 AI 评审结论，只作说明，不是批准")
    assert outputs(world)["reviewComment"]["round"] == 1
    assert result.status is HandoffStatus.OK and result.operation is None and "已直接执行" in result.message
    assert world.issue().status is IssueStatus.PENDING_MERGE and outputs(world)["pendingOperations"] == []
    assert "gates.release-writes 为 auto" in operations.ran[0][1]


def test_release_stops_after_merging_main_for_reverification(tmp_path):
    world, git = to_submit(tmp_path, **DIRECT)
    git.base = git._current()
    git.incoming = [Commit("d" * 40, "li", NOW, "feat: 调整订单")]
    planner = DirectPlanner()
    operations = Operations(planner, git)
    result = releaser(world, git, planner, operations).release(world.issue_id)
    assert [kind for kind, _ in operations.ran] == [OperationKind.MERGE_MAIN]
    assert result.status is HandoffStatus.OK and world.issue().phase is IssuePhase.VERIFY


def test_failed_direct_operations_stop_the_release(tmp_path):
    world, git = to_submit(tmp_path, **DIRECT)
    planner = DirectPlanner()
    result = releaser(world, git, planner, Operations(planner, git, status=OperationStatus.FAILED)).release(
        world.issue_id)
    assert result.status is HandoffStatus.FAILED and "执行失败" in result.message
    assert world.issue().phase is IssuePhase.SUBMIT


def test_by_default_every_write_waits_for_confirmation(tmp_path):
    world, git = to_submit(tmp_path)
    planner = DirectPlanner()
    operations = Operations(planner, git)
    result = releaser(world, git, planner, operations).release(world.issue_id)
    assert result.operation == "OP-0001" and operations.ran == []


class Gh:
    def __init__(self, facts, protected=False, checks=()):
        self.pull = PullState(187, URL, "OPEN")
        self.facts = facts
        self.protected = protected
        self.checks = list(checks)

    def required_checks(self, repo, slug, branch):
        return self.protected

    def pr_checks(self, repo, number):
        return self.checks

    def pr_view(self, repo, ref):
        return self.pull

    def pr_merge_facts(self, repo, number):
        return self.facts


def facts(**changes):
    values = {"state": "OPEN", "draft": False, "mergeable": "MERGEABLE", "merge_state": "CLEAN", "head": HEAD,
              "reviews": ()}
    values.update(changes)
    return MergeFacts(**values)


def reviewing(tmp_path, release=None, **config):
    world, git = to_submit(tmp_path, gates={"merge": "auto"}, **({"release": release} if release else {}), **config)
    service = releaser(world, git, DirectPlanner(), Operations(DirectPlanner(), git))
    service.on_executed(service.deps.planner.plan_push(world.issue_id, None, BRANCH))
    world.event(IssueEvent.PR_CREATED, actor="release", updates={"pr": URL})
    pulls.save(world.conn, PullRecord(world.issue_id, 187, URL, BRANCH, "Fix order 500", "OPEN", NOW))
    return world, git


def test_auto_merge_merges_when_every_condition_holds(tmp_path):
    world, git = reviewing(tmp_path, target={"baseUrl": "https://staging.example.test"})
    planner = DirectPlanner()
    gh = Gh(facts())
    operations = Operations(planner, git, gh)
    report = releaser(world, git, planner, operations, gh).track()
    assert planner.merge == (187, "cty/sample", BRANCH, HEAD, "squash")
    assert world.issue().phase is IssuePhase.DEPLOY_CHECK
    assert outputs(world)["autoMerge"]["merged"] is True and "自动合并 PR #187(squash)" in history(world)
    assert not any("未自动合并" in line for line in report.lines)


def test_auto_merge_waits_and_lists_the_reasons(tmp_path):
    world, git = reviewing(tmp_path)
    planner = DirectPlanner()
    gh = Gh(facts(merge_state="BLOCKED", reviews=({"author": {"login": "cty"}, "state": "CHANGES_REQUESTED"},)))
    service = releaser(world, git, planner, Operations(planner, git, gh), gh)
    report = service.track()
    assert world.issue().status is IssueStatus.PENDING_MERGE and not hasattr(planner, "merge")
    line = next(line for line in report.lines if "未自动合并" in line)
    assert "必需检查或必需评审尚未满足" in line and "cty 请求修改" in line
    assert outputs(world)["autoMerge"]["merged"] is False
    service.track()
    assert history(world).count("未自动合并") == 1


def test_auto_merge_conditions():
    local = {"conclusion": "passed"}
    fix = {"rounds": [{"checksPassed": True, "failures": []}]}
    release = {"push": {"commits": [HEAD]}, "acceptedFindings": []}
    assert auto_merge.blockers(local, fix, release, facts()) == []
    assert auto_merge.blockers({"conclusion": "failed"}, fix, release, facts()) == ["合并前验证的结论不是通过"]
    accepted = {**release, "acceptedFindings": [{"check": "residue"}]}
    assert "修复评审有未处理的阻断意见" in auto_merge.blockers(local, fix, accepted, facts())[0]
    assert auto_merge.blockers(local, fix, release, facts(mergeable="CONFLICTING", merge_state="DIRTY")) == [
        "PR 与主分支冲突", "与主分支冲突"]
    assert auto_merge.blockers(local, fix, release, facts(head="f" * 40)) == [
        "PR 头部 commit 不是本工具最近一次推送的 commit"]
    reviews = ({"author": {"login": "li"}, "state": "CHANGES_REQUESTED"},
               {"author": {"login": "li"}, "state": "APPROVED"})
    assert auto_merge.blockers(local, fix, release, facts(reviews=reviews)) == []


def test_native_auto_merge_is_enabled_once_when_the_branch_requires_checks(tmp_path):
    world, git = reviewing(tmp_path)
    planner = DirectPlanner()
    gh = Gh(facts(merge_state="BLOCKED"), protected=True)
    service = releaser(world, git, planner, Operations(planner, git, gh), gh)
    service.track()
    assert planner.merge == (187, "cty/sample", BRANCH, HEAD, "squash", "auto")
    recorded = outputs(world)["autoMerge"]
    assert (recorded["native"], recorded["enabledFor"], recorded["merged"]) == (True, HEAD, False)
    assert "已为 PR #187 开启 GitHub 自动合并" in history(world)
    del planner.merge
    service.track()
    assert not hasattr(planner, "merge") and world.issue().status is IssueStatus.PENDING_MERGE


def test_failed_required_checks_block_the_native_auto_merge(tmp_path):
    world, git = reviewing(tmp_path)
    planner = DirectPlanner()
    gh = Gh(facts(merge_state="BLOCKED"), protected=True,
            checks=[{"name": "build", "state": "FAILURE", "bucket": "fail"}, {"name": "lint", "bucket": "pass"}])
    report = releaser(world, git, planner, Operations(planner, git, gh), gh).track()
    recorded = outputs(world)["autoMerge"]
    assert not hasattr(planner, "merge") and recorded["ci"] == {"state": "failed", "failed": ["build"], "pending": []}
    assert "CI 必需检查未通过：build" in recorded["reasons"] and any("CI 必需检查未通过" in line for line in report.lines)


def test_without_the_merge_gate_nothing_is_merged(tmp_path):
    world, git = to_submit(tmp_path)
    service = releaser(world, git, DirectPlanner(), Operations(DirectPlanner(), git))
    service.on_executed(service.deps.planner.plan_push(world.issue_id, None, BRANCH))
    world.event(IssueEvent.PR_CREATED, actor="release", updates={"pr": URL})
    pulls.save(world.conn, PullRecord(world.issue_id, 187, URL, BRANCH, "Fix order 500", "OPEN", NOW))
    planner = DirectPlanner()
    gh = Gh(facts())
    releaser(world, git, planner, Operations(planner, git, gh), gh).track()
    assert not hasattr(planner, "merge") and "autoMerge" not in outputs(world)


def test_risky_paths_are_never_merged_automatically_and_get_a_decision_brief(tmp_path):
    from tightrein.store.files import documents

    world, git = reviewing(tmp_path, release={"autoMergeBlockPaths": ["src/Services/"]})
    planner = DirectPlanner()
    gh = Gh(facts())
    report = releaser(world, git, planner, Operations(planner, git, gh), gh).track()
    assert not hasattr(planner, "merge") and world.issue().status is IssueStatus.PENDING_MERGE
    recorded = outputs(world)["autoMerge"]
    assert "改动涉及需要用户决定的路径：src/Services/OrderService.src(src/Services/)" in recorded["reasons"][0]
    brief = documents.read(world.layout.root / recorded["decision"])
    assert brief.header["kind"] == "decision" and brief.blocks["options"][0]["recommended"] is True
    assert any("未自动合并" in line for line in report.lines)


def test_revert_opens_a_pull_request_for_the_user_to_decide(tmp_path):
    world, git = reviewing(tmp_path)
    planner = DirectPlanner()
    service = releaser(world, git, planner, Operations(planner, git))
    assert service.revert(world.issue_id, "回归").status is HandoffStatus.BLOCKED
    world.event(IssueEvent.PR_MERGED, actor="release")
    pulls.save(world.conn, replace(pulls.get(world.conn, world.issue_id), merge_commit="9" * 40))
    result = releaser(world, git, planner, Operations(planner, git)).revert(world.issue_id, "部署后订单查询仍返回 500")
    merge_commit, branch, title, body, mainline = planner.revert
    assert (merge_commit, branch, mainline) == ("9" * 40, f"hotfix/{int(world.issue_id)}-revert-{world.issue().slug}",
                                                False)
    assert title == "Revert: Fix order 500" and "部署后订单查询仍返回 500" in body and "不会自动合并" in body
    assert result.operation is not None and outputs(world)["revert"]["branch"] == branch
    assert world.issue().status is IssueStatus.DONE
    pending = replace(planner.made[result.operation], status=OperationStatus.PENDING)
    pending_operations.save(world.conn, pending.to_record())
    again = releaser(world, git, planner, Operations(planner, git)).revert(world.issue_id, "再次确认")
    assert again.operation == result.operation and "已在等待确认" in again.message
