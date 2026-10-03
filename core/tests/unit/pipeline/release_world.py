"""release 测试共用：待提交的 Issue、按内存状态回答的假 git 与记录调用的假 OperationPlanner。"""

from datetime import datetime, timezone

from fix_world import BASE, SERVICE_PATH, FakeGit
from verify_world import FIXED, fix_handoff, fixed_world

from tightrein.domain.enums import (
    HandoffStatus,
    IssueEvent,
    OperationExecutor,
    OperationKind,
    OperationStatus,
    RunStage,
    Stage,
    VerifyPhase,
)
from tightrein.pipeline.common import stage_runs
from tightrein.pipeline.issue.steps import transitions
from tightrein.vcs.errors import RefNotFound
from tightrein.vcs.operations import OperationDescription, PendingOperation
from tightrein.vcs.parse import Commit

BRANCH = "bugfix/1-order-500"
HEAD = "2" * 40
NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)


class ReleaseGit(FakeGit):
    """在 FakeGit 的基础上模拟远程：incoming 为 origin/main 上的新提交，pushed 为已推送的远程分支。"""

    def __init__(self, root, base):
        super().__init__(root, base)
        self.incoming = []
        self.pushed = False
        self.merging = None
        self.conflicts = ()
        self.versions = {}
        self.fetched = 0

    def fetch(self, repo, prune=False):
        self.fetched += 1

    def remotes(self, repo):
        return {"remote.origin.url": ("git@github.com:cty/sample.git",)}

    def log(self, repo, rev_range, paths=None, limit=None):
        if rev_range.startswith("HEAD..origin/"):
            return list(self.incoming)
        return [Commit("c" * 40, "zhang", NOW, f"change {paths[0]}")] if paths else []

    def oneline(self, repo, rev_range):
        return "2222222 fix: 修复订单查询返回 500\n"

    def rev_parse(self, repo, ref):
        if ref == f"refs/remotes/origin/{BRANCH}" and not self.pushed:
            raise RefNotFound(ref)
        return self.head_commit

    def merge_head(self, repo):
        return self.merging

    def conflict_files(self, repo):
        return tuple(self.conflicts)

    def branches_containing(self, repo, commit, remote=True):
        return ["origin/main"]

    def show(self, repo, rev, path):
        return self.versions.get((rev, path))

    def diff(self, repo, base, head=None, paths=None):
        if head is not None:
            from tightrein.vcs.git_read import Diff, FileDiff

            return Diff((FileDiff(SERVICE_PATH, 1, 0), FileDiff("README.md", 1, 0)))
        return super().diff(repo, base, head, paths)


class Planner:
    def __init__(self, unchanged_pr=False):
        self.calls = []
        self.unchanged_pr = unchanged_pr

    def _operation(self, kind, issue_id, files=(), text="操作", result=None, extra=None):
        self.calls.append((kind, files))
        return PendingOperation(
            id=f"OP-{len(self.calls):04d}", stage=Stage.RELEASE, subject_id=issue_id, kind=kind,
            executor=OperationExecutor.VCS, commands=(), description=OperationDescription(
                "/repo", BRANCH, tuple(files), False, "撤销", text), impact="影响", reversible=True,
            preconditions={"state": extra or {}}, idempotency_key=f"k{len(self.calls)}", confirmations_required=1,
            created_at=NOW, status=OperationStatus.EXECUTED, result=result)

    def plan_commit(self, issue_id, worktree, message, files):
        return self._operation(OperationKind.COMMIT, issue_id, tuple(files), f"提交信息：\n{message}",
                               {"branch": BRANCH, "commit": HEAD})

    def plan_merge_main(self, issue_id, worktree):
        return self._operation(OperationKind.MERGE_MAIN, issue_id, result={"branch": BRANCH, "commit": HEAD},
                               extra={"originMain": "d" * 40})

    def plan_commit_merge(self, issue_id, worktree, files):
        return self._operation(OperationKind.COMMIT_MERGE, issue_id, tuple(files),
                               result={"branch": BRANCH, "commit": HEAD})

    def plan_abort_merge(self, issue_id, worktree):
        return self._operation(OperationKind.ABORT_MERGE, issue_id)

    def plan_push(self, issue_id, worktree, branch):
        return self._operation(OperationKind.PUSH, issue_id, result={"branch": branch, "commit": HEAD})

    def plan_pr_comment(self, issue_id, number, slug, body, key):
        self.comment = body
        return self._operation(OperationKind.PR_COMMENT, issue_id, result={"outputs": [""]})

    def plan_revert_pull_request(self, issue_id, merge_commit, branch, title, body, mainline):
        self.revert = (merge_commit, branch, title, body, mainline)
        return self._operation(OperationKind.REVERT_PULL_REQUEST, issue_id,
                               result={"outputs": ["", "", "", "https://example.test/pull/190"]})

    def plan_pull_request(self, issue_id, worktree, branch, title, body):
        if self.unchanged_pr:
            return None
        self.body = body
        self.title = title
        return self._operation(OperationKind.PULL_REQUEST, issue_id,
                               result={"url": "https://example.test/pull/187"})


def local_outputs(world):
    return {"issueId": world.issue_id, "phase": "local", "target": {"url": "http://localhost:5101", "mode": "api"},
            "commit": BASE, "baseCommit": BASE, "conclusion": "passed", "deferredToStaging": [], "migration": None,
            "unverified": [{"item": "计算节点的流程", "reason": "不在本机启动"}], "consecutiveFailures": 0,
            "items": [{"id": "repro:api-1", "category": "issue-repro", "command": None, "result": "pass",
                       "evidence": [], "reason": None},
                      {"id": "other:0003/api-1", "category": "other-repro", "command": None, "result": "pass",
                       "evidence": [], "reason": None},
                      {"id": "api-shallow", "category": "api-shallow", "command": "api-fuzz", "result": "pass",
                       "evidence": [], "reason": None}]}


def verified(world, phase, outputs):
    run = stage_runs.begin(RunStage.VERIFY, world.layout, world.conn, world.clock, world.events)
    run.handoff(RunStage.VERIFY, world.issue_id, HandoffStatus.OK, outputs, "next", phase=phase)


def to_submit(tmp_path, *, failures=(), branch=BRANCH, **config):
    """修复与合并前验证都已通过的 Issue；worktree 中有改动，git 为 ReleaseGit。"""
    world = fixed_world(tmp_path, **config)
    git = ReleaseGit(world.worktree, world.git().base)
    (world.worktree / SERVICE_PATH).write_text(FIXED, encoding="utf-8")
    rounds = [{"round": 1, "checksPassed": not failures, "reviews": [], "blockerCategories": [], "risk": None,
               "failures": list(failures), "discardedFindings": []}]
    fix_handoff(world, git, changedFiles=[{"path": SERVICE_PATH, "added": 1, "removed": 1}], rounds=rounds,
                summary="在 OrderService.Get 中按公司过滤", userVisibleChange="无", branch=branch)
    env = transitions.IssueEnv(world.conn, world.layout, world.clock, world.config)
    transitions.annotate(env, world.issue_id, "建立修复分支", updates={"branch": branch})
    for event in (IssueEvent.FIX_DONE, IssueEvent.VERIFY_PASSED):
        world.event(event, actor="test")
    verified(world, VerifyPhase.LOCAL, local_outputs(world))
    return world, git

