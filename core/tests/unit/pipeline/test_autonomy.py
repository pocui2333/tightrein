"""自主决定：新建 Issue 的自动放行与需要用户决定、规则本身、GitHub 镜像的 needs-decision 标签。"""

from pipeline_world import PROJECT, make_signal
from test_issue_github import FakeGithub
from triage_world import make_triage_world, store_triaged, triage_outputs

from tightrein.domain.enums import HandoffStatus, IssueEvent, IssueStatus, RunStage, Stage
from tightrein.domain.issue import Hold, IssueContext
from tightrein.observability.redact import Redactor
from tightrein.orchestrator.policy import autonomy
from tightrein.pipeline.common import stage_runs
from tightrein.pipeline.issue.service import IssueDeps, IssueService
from tightrein.pipeline.issue.steps import transitions
from tightrein.pipeline.issue.steps.github import GithubMirror
from tightrein.store.repos import issue_events, issues
from tightrein.vcs.gh_issues import GhIssues
from tightrein.vcs.process import VcsProcess

AUTONOMY = {"issue-approve": "auto", "plan-confirm": "auto", "fix-session": "auto"}
FLAGS = {"design": {"flagged": False}, "dataStructure": {"flagged": False}, "publicContract": {"flagged": False}}


class Prepared:
    def __init__(self):
        self.calls = []

    def __call__(self, issue_id):
        self.calls.append(issue_id)
        return type("Result", (), {"status": HandoffStatus.BLOCKED, "message": "等待确认"})()


def risky_outputs():
    outputs = triage_outputs("P-0001", labels=["discuss-with-author"])
    outputs["evidence"] = {**outputs["evidence"], "impact": {**outputs["evidence"]["impact"], "kind": "authorization"}}
    return outputs


def created(tmp_path, outputs=None, mirror=None, **config):
    world = make_triage_world(tmp_path, **config)
    store_triaged(world, "P-0001", make_signal(1), outputs=outputs or triage_outputs("P-0001"))
    prepare = Prepared()
    service = IssueService(IssueDeps(world.layout, world.config, world.conn, world.clock, world.events,
                                     snapshot=world.worktree, mirror=mirror(world) if mirror else None,
                                     prepare=prepare))
    run = service.create()
    return world, run.items[0].issue_id, prepare


def text_of(world, issue_id):
    return (world.layout.root / issues.get(world.conn, issue_id).path).read_text(encoding="utf-8")


def handoff_outputs(world, problem_id="P-0001"):
    return stage_runs.latest_outputs(world.conn, world.layout, RunStage.ISSUE, problem_id)[1]


def test_simple_issues_are_approved_and_prepared(tmp_path):
    world, issue_id, prepare = created(tmp_path, gates=AUTONOMY)
    assert issues.get(world.conn, issue_id).issue.status is IssueStatus.TODO and prepare.calls == [issue_id]
    assert "自动放行：满足" in text_of(world, issue_id) and "复杂度为低" in text_of(world, issue_id)
    assert [event.actor for event in issue_events.for_issue(world.conn, issue_id)][-1] == autonomy.ACTOR
    assert handoff_outputs(world)["autonomy"]["approved"] is True


def test_issues_needing_decisions_stay_in_review(tmp_path):
    world, issue_id, prepare = created(tmp_path, risky_outputs(), gates=AUTONOMY)
    assert issues.get(world.conn, issue_id).issue.status is IssueStatus.NEEDS_DECISION and prepare.calls == []
    assert "需要用户决定：分诊标注「需要先与代码作者讨论」；影响类别为权限" in text_of(world, issue_id)
    assert handoff_outputs(world)["autonomy"] == {
        "approved": False, "reasons": ["分诊标注「需要先与代码作者讨论」", "影响类别为权限"]}


def test_without_autonomy_issues_wait_for_the_user(tmp_path):
    world, issue_id, prepare = created(tmp_path)
    assert issues.get(world.conn, issue_id).issue.status is IssueStatus.NEEDS_DECISION and prepare.calls == []
    assert "autonomy" not in handoff_outputs(world)


def test_approval_rules(make_config):
    config = make_config(autonomy={"approve": {"maxComplexity": "medium", "severities": ["P0"]}})
    assert autonomy.approval(config, [triage_outputs(complexity="medium")]).approved
    decision = autonomy.approval(config, [triage_outputs(complexity="high", severity="P0",
                                                         flags={**FLAGS, "design": {"flagged": True}})])
    assert decision.reasons == ("需用户定夺：根因在设计本身", "复杂度为高，高于中", "严重度 P0 需要用户决定")
    assert autonomy.approval(config, []).reasons == ("没有分诊结论",)


def test_plan_rules(make_config):
    config = make_config()
    plan = {"estimate": {"files": 3, "lines": 20}, "userDecisions": [], "flags": FLAGS, "protectedTouches": [],
            "migration": None, "newDependencies": [], "deletions": [], "split": None}
    assert autonomy.plan_confirmation(config, plan, None).approved
    split = {"reason": "超出上限", "followUps": [{"title": "清理", "goal": "删去旧调用", "files": ["a.py"],
                                                 "estimate": {"files": 1, "lines": 10}, "acceptance": ["没有旧调用"]}]}
    assert autonomy.plan_confirmation(config, {**plan, "split": split}, None).reasons[-1] == "拆分为 2 个子任务，本计划只做第 1 个"
    assert autonomy.plan_confirmation(config, {**plan, "estimate": {"files": 3, "lines": 21}}, None).reasons == (
        "预估改动 3 个文件、21 行，超过 3 个文件、20 行",)
    conflicted = {"design": {"planConflicts": ["计划没有列出空状态"]}}
    changed = {**plan, "migration": {"entries": ["001"], "reversible": True, "revertMethod": "drop"},
               "deletions": [{"path": "a.py", "reason": "废弃"}]}
    assert autonomy.plan_confirmation(config, changed, conflicted).reasons == (
        "有数据结构变更(迁移)", "删除文件 a.py", "前端设计说明与计划有冲突：计划没有列出空状态")


def test_plans_touching_permissions_or_secrets_always_go_to_the_user(make_config):
    config = make_config(review={"riskRules": {"authz": {"paths": ["src/auth/"], "patterns": []}}})
    plan = {"estimate": {"files": 2, "lines": 20}, "userDecisions": [], "flags": FLAGS, "protectedTouches": [],
            "migration": None, "newDependencies": [], "deletions": [], "split": None,
            "files": [{"path": "src/auth/roles.py", "isNew": False}, {"path": "config/.env.local", "isNew": True}]}
    decision = autonomy.plan_confirmation(config, plan, None)
    assert not decision.approved and len(decision.reasons) == 2
    assert all("gates.permissions-secrets" in reason for reason in decision.reasons)


def test_the_mirror_marks_issues_that_need_a_decision(tmp_path):
    gh = FakeGithub()
    repo = tmp_path / "repo"
    repo.mkdir()

    def mirror(world):
        process = VcsProcess(execute=gh, environ={}, sleep=lambda seconds: None)
        env = transitions.IssueEnv(world.conn, world.layout, world.clock, world.config)
        remotes = {"remote.origin.url": ("git@github.com:owner/name.git",)}
        return GithubMirror(env, process, GhIssues(process, repo, 100), lambda path: remotes, Redactor(), "R-1")

    world, issue_id, _ = created(tmp_path, risky_outputs(), mirror, gates={**AUTONOMY, "mirror-writes": "auto"},
                                 project={**PROJECT["project"], "repo": str(repo)}, issues={"tracker": "github"})
    item = gh.issues[1]
    assert item["labels"] == {"bug"}
    assert "bug" in gh.labels
    service = IssueService(IssueDeps(world.layout, world.config, world.conn, world.clock, world.events,
                                     mirror=mirror(world)))
    service.approve(issue_id)
    assert item["labels"] == {"bug"}
    env = transitions.IssueEnv(world.conn, world.layout, world.clock, world.config)
    hold = Hold("无人值守修复停下", Stage.FIX, world.clock.now(), "评审无法判断")
    transitions.apply_event(env, transitions.record_of(env, issue_id), IssueEvent.FIX_HELD, IssueContext(hold=hold),
                            note="无人值守修复停下")
    service.mirror([issue_id])
    assert item["labels"] == {"bug"}
    service.approve(issue_id, note="用户处理完毕")
    assert item["labels"] == {"bug"}
    assert item["comments"] == []
