from datetime import timedelta

import pytest
from vcs_world import RUN, VcsWorld

from tightrein.domain.enums import OperationExecutor, OperationKind, OperationStatus, Stage
from tightrein.observability import events
from tightrein.observability.events import EventLog
from tightrein.observability.redact import Redactor
from tightrein.observability.tracing import Tracer
from tightrein.store import idempotency
from tightrein.vcs.errors import MergeConflict, PushRejected, WorktreeDirty
from tightrein.vcs.executor import FollowUp, OperationRunner, OperationStateError
from tightrein.vcs.operations import OperationDescription, PendingOperation, load, save


@pytest.fixture
def world(repos, make_config, tmp_path):
    world = VcsWorld(repos, make_config, tmp_path)
    yield world
    world.conn.close()


def test_confirm_then_execute_creates_the_fix_worktree(world):
    operation = world.planner.plan_create_fix_worktree("0007", "cty/fix-order")
    result = world.confirm_and_execute(operation)
    worktree = world.layout.fix_worktree("0007")
    assert (result.status, result.ran) == (OperationStatus.EXECUTED, True)
    assert result.operation.result == {"worktree": str(worktree), "branch": "cty/fix-order"}
    assert world.git.head(worktree).branch == "cty/fix-order"
    assert (worktree / ".claude").is_symlink()
    assert (world.layout.vcs_raw_dir(RUN, operation.id) / "step-1.log").read_text(encoding="utf-8").startswith("$ git")
    assert idempotency.get(world.conn, "worktree:0007").status == idempotency.DONE


def test_an_existing_matching_worktree_is_reused(world):
    world.fix_worktree()
    operation = world.planner.plan_create_fix_worktree("0007", "cty/fix-order")
    result = world.confirm_and_execute(operation)
    assert (result.status, result.ran, result.operation.result["reused"]) == (OperationStatus.EXECUTED, False, True)


def test_execution_needs_confirmation(world):
    operation = world.planner.plan_init_readonly_worktree()
    with pytest.raises(OperationStateError):
        world.runner.execute(operation.id, clock=world.clock)
    world.runner.confirm(operation.id, confirmed_by="cty", clock=world.clock)
    with pytest.raises(OperationStateError):
        world.runner.confirm(operation.id, confirmed_by="cty", clock=world.clock)


def test_rejected_operations_run_the_follow_up(world):
    rejected = []
    runner = OperationRunner(world.conn, world.process, world.git, world.gh_reader, world.layout, RUN,
                             follow_ups={Stage.FIX: FollowUp(rejected=rejected.append)})
    operation = world.planner.plan_create_fix_worktree("0007", "cty/fix-order")
    result = runner.reject(operation.id, note="先不修", clock=world.clock)
    assert (result.status, result.result) == (OperationStatus.REJECTED, {"note": "先不修"})
    assert [item.id for item in rejected] == [operation.id]
    with pytest.raises(OperationStateError):
        runner.confirm(operation.id, confirmed_by="cty", clock=world.clock)


def test_cleanup_needs_two_confirmations(world):
    worktree = world.fix_worktree()
    operation = world.planner.plan_cleanup("0007", worktree, "cty/fix-order")
    first = world.runner.confirm(operation.id, confirmed_by="cty", clock=world.clock)
    assert (first.status, first.confirmations_given) == (OperationStatus.PENDING, 1)
    with pytest.raises(OperationStateError):
        world.runner.execute(operation.id, clock=world.clock)
    second = world.runner.confirm(operation.id, confirmed_by="cty", clock=world.clock)
    assert second.status is OperationStatus.CONFIRMED
    result = world.runner.execute(operation.id, clock=world.clock)
    assert result.status is OperationStatus.EXECUTED
    assert not worktree.exists()
    assert not world.git.branch_exists(world.repo, "cty/fix-order")


def test_a_dirty_worktree_is_not_removed(world):
    worktree = world.fix_worktree()
    operation = world.planner.plan_cleanup("0007", worktree, "cty/fix-order")
    world.repos.write(worktree, "src/OrderService.cs", "未提交\n")
    result = world.confirm_and_execute(operation)
    assert result.status is OperationStatus.FAILED
    assert isinstance(result.error, WorktreeDirty)
    assert result.operation.result["dirtyPaths"] == ["src/OrderService.cs"]
    assert worktree.exists()


def test_changed_preconditions_expire_the_operation(world):
    worktree = world.fix_worktree()
    world.repos.write(worktree, "src/OrderService.cs", "fixed\n")
    operation = world.planner.plan_commit("0007", worktree, "fix: 订单查询 500", ["src/OrderService.cs"])
    world.repos.write(worktree, "src/OrderService.cs", "changed after planning\n")
    head = world.git.head(worktree).commit
    result = world.confirm_and_execute(operation)
    assert (result.status, result.ran, result.operation.result) == (
        OperationStatus.EXPIRED, False, {"changed": ["diffHash"]},
    )
    assert world.git.head(worktree).commit == head
    assert idempotency.get(world.conn, operation.idempotency_key) is None


def test_commit_push_and_pull_request(world):
    worktree = world.fix_worktree()
    world.repos.write(worktree, "src/OrderService.cs", "fixed\n")
    commit = world.confirm_and_execute(world.planner.plan_commit("0007", worktree, "fix: 订单查询 500",
                                                                 ["src/OrderService.cs"]))
    head = world.git.head(worktree).commit
    assert commit.operation.result == {"branch": "cty/fix-order", "commit": head}
    assert world.git.log(worktree, "HEAD", limit=1)[0].subject == "fix: 订单查询 500"
    push = world.confirm_and_execute(world.planner.plan_push("0007", worktree, "cty/fix-order"))
    assert push.status is OperationStatus.EXECUTED
    assert world.git.rev_parse(worktree, "origin/cty/fix-order") == head
    pull = world.confirm_and_execute(world.planner.plan_pull_request("0007", worktree, "cty/fix-order", "Fix", "描述\n"))
    assert pull.operation.result == {"url": "https://github.com/cty/sample/pull/186"}
    assert world.gh.pulls[186]["body"] == "描述\n"


def test_the_same_key_runs_only_once(world):
    worktree = world.fix_worktree()
    world.repos.write(worktree, "src/OrderService.cs", "fixed\n")
    first = world.planner.plan_commit("0007", worktree, "fix: once", ["src/OrderService.cs"])
    second = world.planner.plan_commit("0007", worktree, "fix: once", ["src/OrderService.cs"])
    assert first.idempotency_key == second.idempotency_key
    world.confirm_and_execute(first)
    result = world.confirm_and_execute(second)
    assert (result.status, result.ran, result.operation.result["alreadyExecuted"]) == (
        OperationStatus.EXECUTED, False, True,
    )
    assert len(world.git.log(worktree, "origin/main..HEAD")) == 1


def test_an_interrupted_execution_is_reconciled(world):
    worktree = world.fix_worktree()
    world.repos.write(worktree, "src/OrderService.cs", "fixed\n")
    operation = world.planner.plan_commit("0007", worktree, "fix: crash", ["src/OrderService.cs"])
    world.runner.confirm(operation.id, confirmed_by="cty", clock=world.clock)
    idempotency.begin(world.conn, operation.idempotency_key, world.clock)
    world.repos.git(worktree, "add", "src/OrderService.cs")
    world.repos.git(worktree, "commit", "-q", "-m", "fix: crash")
    result = world.runner.execute(operation.id, clock=world.clock)
    assert (result.status, result.ran, result.operation.result["alreadyExecuted"]) == (
        OperationStatus.EXECUTED, False, True,
    )
    assert idempotency.get(world.conn, operation.idempotency_key).status == idempotency.DONE


def test_an_interrupted_execution_that_did_nothing_runs_again(world):
    worktree = world.fix_worktree()
    world.repos.write(worktree, "src/OrderService.cs", "fixed\n")
    operation = world.planner.plan_commit("0007", worktree, "fix: retry", ["src/OrderService.cs"])
    world.runner.confirm(operation.id, confirmed_by="cty", clock=world.clock)
    idempotency.begin(world.conn, operation.idempotency_key, world.clock)
    result = world.runner.execute(operation.id, clock=world.clock)
    assert (result.status, result.ran) == (OperationStatus.EXECUTED, True)


def test_merge_conflicts_are_reported(world):
    worktree = world.fix_worktree()
    world.repos.commit(worktree, "fix: local", {"README.md": "local\n"})
    world.upstream_commit({"README.md": "upstream\n"})
    result = world.confirm_and_execute(world.planner.plan_merge_main("0007", worktree))
    assert result.status is OperationStatus.FAILED
    assert isinstance(result.error, MergeConflict)
    assert result.operation.result["conflictFiles"] == ["README.md"]
    world.repos.write(worktree, "README.md", "resolved\n")
    merged = world.confirm_and_execute(world.planner.plan_commit_merge("0007", worktree, ["README.md"]))
    assert merged.status is OperationStatus.EXECUTED
    assert world.git.merge_head(worktree) is None


def test_abort_merge(world):
    worktree = world.fix_worktree()
    before = world.repos.commit(worktree, "fix: local", {"README.md": "local\n"})
    world.upstream_commit({"README.md": "upstream\n"})
    world.confirm_and_execute(world.planner.plan_merge_main("0007", worktree))
    result = world.confirm_and_execute(world.planner.plan_abort_merge("0007", worktree))
    assert result.status is OperationStatus.EXECUTED
    assert world.git.head(worktree).commit == before


def test_rejected_pushes_are_reported(world):
    worktree = world.fix_worktree()
    world.repos.commit(worktree, "fix: one", {"a.txt": "a"})
    world.confirm_and_execute(world.planner.plan_push("0007", worktree, "cty/fix-order"))
    other = world.repos.root / "other-fix"
    world.repos.git(world.repos.root, "clone", "-q", "-b", "cty/fix-order", str(world.origin), str(other))
    world.repos.commit(other, "fix: elsewhere", {"b.txt": "b"})
    world.repos.git(other, "push", "-q", "origin", "cty/fix-order")
    world.repos.commit(worktree, "fix: two", {"c.txt": "c"})
    result = world.confirm_and_execute(world.planner.plan_push("0007", worktree, "cty/fix-order"))
    assert result.status is OperationStatus.FAILED
    assert isinstance(result.error, PushRejected)
    assert result.operation.result["rejected"] == ["refs/heads/cty/fix-order:refs/heads/cty/fix-order"]


def test_confirmations_are_user_action_events(world):
    tracer = Tracer(EventLog(world.layout, Redactor()), world.clock, run_id=RUN, stage="release")
    runner = OperationRunner(world.conn, world.process, world.git, world.gh_reader, world.layout, RUN, tracer=tracer)
    operation = world.planner.plan_init_readonly_worktree()
    runner.confirm(operation.id, confirmed_by="cty", clock=world.clock)
    written = events.read(world.layout.events_log(world.clock.now().date()))
    assert [(event.operation, event.decision, event.artifact) for event in written] == [
        ("user_action", "confirm", operation.id),
    ]


def test_stale_operations_expire_after_seven_days(world):
    operation = world.planner.plan_init_readonly_worktree()
    world.clock.advance(timedelta(days=7))
    assert world.runner.expire_stale(world.clock) == []
    world.clock.advance(timedelta(seconds=1))
    assert [item.id for item in world.runner.expire_stale(world.clock)] == [operation.id]
    assert load(world.conn, operation.id).status is OperationStatus.EXPIRED


def test_operations_without_commands_are_marked_executed(world):
    executed = []
    runner = OperationRunner(world.conn, world.process, world.git, world.gh_reader, world.layout, RUN,
                             follow_ups={Stage.FIX: FollowUp(executed=executed.append)})
    plan = PendingOperation(
        "OP-0098", Stage.FIX, "0007", OperationKind.FIX_PLAN, OperationExecutor.VCS, (),
        OperationDescription(str(world.repo), None, (), False, "重新执行 fix plan", "确认修复计划"), "确认后开始修复",
        True, {}, "fix-plan:0007", 1, world.clock.now(),
    )
    save(world.conn, plan)
    runner.confirm("OP-0098", confirmed_by="cty", clock=world.clock)
    result = runner.execute("OP-0098", clock=world.clock)
    assert (result.status, result.operation.result) == (OperationStatus.EXECUTED, {})
    assert [item.id for item in executed] == ["OP-0098"]


def test_github_issue_operations_return_each_output(world):
    executed = []
    runner = OperationRunner(world.conn, world.process, world.git, world.gh_reader, world.layout, RUN,
                             follow_ups={Stage.ISSUE: FollowUp(executed=executed.append)})
    operation = world.planner.plan_github_issue(
        "0007", [(("issue", "create", "--repo", "cty/sample", "--title", "t", "--body-file", "{raw}/body.md"), "建"),
                 (("issue", "comment", "1", "--repo", "cty/sample", "--body-file", "{raw}/c.md"), "评论")],
        {"body.md": "正文\n", "c.md": "评论\n"}, "同步 0007", "github:0007:x", {"effects": []})
    assert operation.preconditions["state"] == {} and operation.stage is Stage.ISSUE
    runner.confirm(operation.id, confirmed_by="cty", clock=world.clock)
    result = runner.execute(operation.id, clock=world.clock)
    assert result.operation.result == {"outputs": ["https://github.com/cty/sample/issues/1", ""]}
    assert world.gh.issues == ["正文\n"] and [item.id for item in executed] == [operation.id]


def test_unattended_operations_run_without_confirmation_and_leave_a_gate_event(world):
    tracer = Tracer(EventLog(world.layout, Redactor()), world.clock, run_id=RUN, stage="release")
    runner = OperationRunner(world.conn, world.process, world.git, world.gh_reader, world.layout, RUN, tracer=tracer)
    worktree = world.fix_worktree()
    world.repos.write(worktree, "src/OrderService.cs", "fixed\n")
    operation = world.planner.plan_commit("0007", worktree, "fix: 订单", ["src/OrderService.cs"])
    result = runner.run_unattended(operation.id, reason="gates.release-writes 为 auto", clock=world.clock)
    assert (result.status, result.operation.confirmations_given) == (OperationStatus.EXECUTED, 1)
    assert world.git.log(worktree, "HEAD", limit=1)[0].subject == "fix: 订单"
    written = events.read(world.layout.events_log(world.clock.now().date()))
    assert [(event.operation, event.decision, event.reason) for event in written] == [
        ("gate", "auto-confirm", "gates.release-writes 为 auto")]


def test_unattended_execution_refuses_cleanup_and_the_main_branch(world):
    worktree = world.fix_worktree()
    cleanup = world.planner.plan_cleanup("0007", worktree, "cty/fix-order")
    with pytest.raises(OperationStateError):
        world.runner.run_unattended(cleanup.id, reason="x", clock=world.clock)
    push = world.planner.plan_push("0008", world.repo, "main")
    with pytest.raises(OperationStateError):
        world.runner.run_unattended(push.id, reason="x", clock=world.clock)
    assert load(world.conn, push.id).status is OperationStatus.PENDING


def test_merging_a_pull_request_deletes_only_the_remote_branch(world):
    world.gh.pulls[186] = {"head": "cty/fix-order", "state": "OPEN", "body": ""}
    operation = world.planner.plan_merge_pull_request("0007", 186, "cty/sample", "cty/fix-order", "a" * 40, "squash")
    assert operation.kind is OperationKind.MERGE_PULL_REQUEST and operation.preconditions["state"] == {}
    result = world.runner.run_unattended(operation.id, reason="满足合并条件", clock=world.clock)
    assert (result.status, result.operation.result) == (OperationStatus.EXECUTED, {"outputs": [""]})
    assert world.gh.calls[-1] == ("gh", "pr", "merge", "186", "--squash", "--delete-branch", "--match-head-commit",
                                  "a" * 40, "--repo", "cty/sample")
    assert world.gh.pulls[186]["state"] == "MERGED"


def test_auto_merge_comments_and_reverts_are_written_through_gh_and_git(world):
    world.gh.pulls[186] = {"head": "cty/fix-order", "state": "OPEN", "body": ""}
    native = world.planner.plan_merge_pull_request("0007", 186, "cty/sample", "cty/fix-order", "a" * 40, "squash",
                                                   auto=True)
    world.runner.run_unattended(native.id, reason="必需检查通过后由 GitHub 合并", clock=world.clock)
    assert "--auto" in world.gh.calls[-1] and native.idempotency_key.endswith(":auto")
    comment = world.planner.plan_pr_comment("0007", 186, "cty/sample", "AI 评审：通过", "review-comment:186:1")
    assert world.runner.run_unattended(comment.id, reason="x", clock=world.clock).status is OperationStatus.EXECUTED
    assert world.gh.pulls[186]["comments"] == ["AI 评审：通过\n"]
    merged = world.upstream_commit({"src/order.txt": "fixed\n"})
    revert = world.planner.plan_revert_pull_request("0007", merged, "hotfix/7-revert-order", "Revert: 修复订单",
                                                    "撤销原因", mainline=False)
    assert revert.kind is OperationKind.REVERT_PULL_REQUEST
    assert revert.preconditions["state"] == {"branchExists": False, "worktreeExists": False}
    result = world.runner.run_unattended(revert.id, reason="部署后确认发现回归", clock=world.clock)
    assert result.status is OperationStatus.EXECUTED and result.operation.result["outputs"][-1].endswith("/pull/187")
    assert world.gh.pulls[187]["head"] == "hotfix/7-revert-order"
    worktree = world.layout.fix_worktree("0007").with_name("revert-0007")
    assert not (worktree / "src" / "order.txt").exists()
