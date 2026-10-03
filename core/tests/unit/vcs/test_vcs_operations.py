import pytest
from vcs_world import RUN, VcsWorld

from tightrein.contracts import validate
from tightrein.domain.enums import OperationExecutor, OperationKind, OperationStatus, Stage
from tightrein.store.repos import pending_operations
from tightrein.vcs.errors import VcsError
from tightrein.vcs.operations import PendingOperation, describe, load, sorted_files


@pytest.fixture
def world(repos, make_config, tmp_path):
    world = VcsWorld(repos, make_config, tmp_path)
    yield world
    world.conn.close()


def test_create_fix_worktree_is_planned_from_origin_main(world):
    operation = world.planner.plan_create_fix_worktree("0007", "cty/fix-order")
    base = world.repos.head(world.repo)
    worktree = world.layout.fix_worktree("0007")
    assert (operation.id, operation.kind, operation.stage, operation.executor) == (
        "OP-0001", OperationKind.CREATE_FIX_WORKTREE, Stage.FIX, OperationExecutor.VCS,
    )
    assert [step.argv for step in operation.commands] == [
        ("git", "worktree", "add", "-b", "cty/fix-order", str(worktree), base),
        ("ln", "-s", str(world.repo / ".claude"), str(worktree / ".claude")),
    ]
    assert operation.idempotency_key == "worktree:0007"
    assert operation.preconditions["state"] == {"branchExists": False, "worktreeExists": False}
    assert operation.preconditions["base"] == base
    assert (operation.status, operation.confirmations_required, operation.description.affects_remote) == (
        OperationStatus.PENDING, 1, False,
    )
    assert load(world.conn, "OP-0001") == operation


def test_operations_follow_the_schema_and_round_trip_through_the_table(world):
    operation = world.planner.plan_init_readonly_worktree()
    data = operation.to_dict()
    assert validate.validate("data/pending-operation.schema.json", data) == []
    record = pending_operations.get(world.conn, operation.id)
    assert PendingOperation.from_record(record) == operation
    assert (operation.subject_id, operation.idempotency_key) == ("sample", "readonly-worktree")
    assert operation.commands[0].argv[:4] == ("git", "worktree", "add", "--detach")


def test_init_uses_a_local_origin_main_without_fetching(world):
    world.repos.git(world.repo, "remote", "set-url", "origin", str(world.layout.root / "unreachable.git"))
    operation = world.planner.plan_init_readonly_worktree()
    assert operation.preconditions["base"] == world.git.rev_parse(world.repo, "origin/main")


def test_commit_writes_the_message_file(world):
    worktree = world.fix_worktree()
    world.repos.write(worktree, "src/OrderService.cs", "fixed\n")
    world.repos.write(worktree, "src/Api/OrderController.cs", "fixed\n")
    operation = world.planner.plan_commit("0007", worktree, "fix: 订单查询 500", ["src/OrderService.cs",
                                                                                "src/Api/OrderController.cs"])
    raw = world.layout.vcs_raw_dir(RUN, operation.id)
    assert [step.argv for step in operation.commands] == [
        ("git", "add", "--", "src/Api/OrderController.cs", "src/OrderService.cs"),
        ("git", "commit", "-F", str(raw / "commit-message.txt")),
    ]
    assert (raw / "commit-message.txt").read_text(encoding="utf-8") == "fix: 订单查询 500\n"
    diff_hash = operation.preconditions["state"]["diffHash"]
    assert operation.idempotency_key == f"commit:0007:{diff_hash}"
    assert operation.description.files == ("src/Api/OrderController.cs", "src/OrderService.cs")
    assert operation.preconditions["state"]["branch"] == "cty/fix-order"
    with pytest.raises(ValueError):
        world.planner.plan_commit("0007", worktree, "x", [])


def test_files_are_sorted_by_file_name():
    assert sorted_files(["src/b/A.cs", "src/a/B.cs", "src/a/A.cs", "src/a/A.cs"]) == (
        "src/a/A.cs", "src/b/A.cs", "src/a/B.cs",
    )


def test_merge_push_and_cleanup(world):
    worktree = world.fix_worktree()
    upstream = world.upstream_commit({"README.md": "upstream\n"})
    merge = world.planner.plan_merge_main("0007", worktree)
    assert merge.commands[0].argv == ("git", "merge", "--no-ff", "--no-edit", "origin/main")
    assert merge.preconditions["state"]["originMain"] == upstream
    assert merge.idempotency_key == f"merge:cty/fix-order:{upstream}"
    push = world.planner.plan_push("0007", worktree, "cty/fix-order")
    assert push.commands[0].argv == ("git", "push", "--porcelain", "-u", "origin", "cty/fix-order")
    assert push.description.affects_remote and not push.reversible
    cleanup = world.planner.plan_cleanup("0007", worktree, "cty/fix-order")
    assert cleanup.confirmations_required == 2
    assert [step.argv[1] for step in cleanup.commands] == ["worktree", "branch", "fetch"]
    with pytest.raises(VcsError):
        world.planner.plan_abort_merge("0007", worktree)


def test_pull_request_is_created_or_updated(world):
    worktree = world.fix_worktree()
    create = world.planner.plan_pull_request("0007", worktree, "cty/fix-order", "Fix order query 500", "## 1. 问题\n")
    raw = world.layout.vcs_raw_dir(RUN, create.id)
    assert create.commands[0].argv == (
        "gh", "pr", "create", "--base", "main", "--head", "cty/fix-order", "--title", "Fix order query 500",
        "--body-file", str(raw / "pr-body.md"),
    )
    assert create.preconditions["state"]["pullRequest"] is None
    world.gh.pulls[186] = {"head": "cty/fix-order", "state": "OPEN", "body": "## 1. 问题\n"}
    assert world.planner.plan_pull_request("0007", worktree, "cty/fix-order", "t", "## 1. 问题\n") is None
    edit = world.planner.plan_pull_request("0007", worktree, "cty/fix-order", "t", "## 1. 问题\n补充\n")
    assert edit.commands[0].argv[:4] == ("gh", "pr", "edit", "186")
    assert "更新 PR #186" in edit.description.text


def test_describe_lists_everything_the_user_confirms(world):
    worktree = world.fix_worktree()
    cleanup = world.planner.plan_cleanup("0007", worktree, "cty/fix-order")
    text = describe(cleanup)
    assert text.splitlines()[0] == "OP-0001 删除修复 worktree 与本地分支(cleanup)，对象 0007"
    for fragment in (f"git worktree remove {worktree}", "git branch -d cty/fix-order", "分支：cty/fix-order",
                     "是否影响远程：否", "能否撤销：能", "需要确认 2 次；已确认 0 次"):
        assert fragment in text
