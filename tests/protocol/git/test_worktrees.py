"""worktree：从 origin/main 新建与复用、只读 worktree 的游离 HEAD 切换与 chmod 锁定、清理先确认干净只用 branch -d。"""

import os
import stat
from pathlib import Path

import pytest

from tightrein.protocol.git import worktrees
from tightrein.protocol.git.git import CommandFailed, Head, NetworkError, RefNotFound, WorktreeDirty, WriteScope
from tightrein.protocol.git.worktrees import ReadonlyLocked, ReadonlyRestoreFailed


@pytest.fixture
def readonly(clone, scope, tmp_path):
    """只读 worktree 停在第一个提交，origin/main 已前进到第二个：(repos, git, 路径, 标记, 第一个, 第二个)。"""
    repos, _, repo, git = clone
    first = repos.head(repo)
    path = tmp_path / "ws" / "worktrees" / "readonly"
    worktrees.create_readonly(git, path, scope=scope)
    second = repos.commit(repo, "feat: second", {"src/New.cs": "class New {}\n"})
    repos.git(repo, "push", "-q", "origin", "main")
    return repos, git, path, tmp_path / "ws" / "data" / "readonly-readonly.json", first, second


def test_a_fix_worktree_is_created_from_origin_main_and_reused(clone, scope, tmp_path):
    repos, origin, repo, git = clone
    repos.write(repo, ".claude/settings.json", "{}\n")
    upstream = repos.upstream(origin, "feat: upstream", {"README.md": "upstream\n"})
    path = tmp_path / "ws" / "worktrees" / "0007"
    worktrees.create_fix(git, path, "cty/fix-order", links=[".claude/"], scope=scope)
    fixed = git.at(path)
    assert (fixed.head().branch, fixed.head().commit) == ("cty/fix-order", upstream)
    assert (path / ".claude").is_symlink() and (path / ".claude" / "settings.json").exists()
    assert git.head().branch == "main" and repos.head(repo) != upstream
    worktrees.create_fix(git, path, "cty/fix-order", links=[".claude/"], scope=scope)
    assert git.at(path).head().branch == "cty/fix-order"


def test_an_existing_matching_worktree_is_reused_without_a_record(clone, conn, clock, tmp_path):
    """中断后重来：目录与分支已在(上次没来得及记完成)时直接复用，不报错。"""
    repos, _, repo, git = clone
    path = tmp_path / "ws" / "worktrees" / "0007"
    repos.git(repo, "worktree", "add", "-q", "-b", "cty/fix-order", str(path), "origin/main")
    worktrees.create_fix(git, path, "cty/fix-order", scope=WriteScope(conn, clock, "0007", "implement.prepare"))
    assert git.at(path).head().branch == "cty/fix-order"


def test_sync_moves_the_detached_head(readonly):
    _, git, path, marker, first, second = readonly
    result = worktrees.sync_readonly(git, path, marker=marker)
    assert (result.previous, result.commit, result.moved) == (first, second, True)
    assert git.at(path).head() == Head(second, None)
    again = worktrees.sync_readonly(git, path, marker=marker, commit=first)
    assert (again.commit, git.at(path).head().branch) == (first, None)
    assert not worktrees.sync_readonly(git, path, marker=marker, commit=first).moved
    assert git.head().branch == "main"
    listed = {Path(item.path).name: item for item in git.worktree_list()}
    assert listed["readonly"].detached and listed["readonly"].branch is None


def test_the_target_commit_may_be_an_abbreviated_prefix(readonly):
    _, git, path, marker, first, second = readonly
    worktrees.sync_readonly(git, path, marker=marker, commit=second)
    again = worktrees.sync_readonly(git, path, marker=marker, commit=second[:7])
    assert (again.previous, again.commit, again.moved) == (second, second, False)
    back = worktrees.sync_readonly(git, path, marker=marker, commit=first[:10])
    assert (back.commit, back.moved, git.at(path).head().commit) == (first, True, first)


def test_build_products_do_not_block_the_switch(readonly):
    repos, git, path, marker, _, second = readonly
    repos.write(path, "bin/Debug/app.dll", "binary")
    assert worktrees.sync_readonly(git, path, marker=marker).commit == second


def test_a_dirty_readonly_worktree_is_not_switched_or_cleaned(readonly):
    repos, git, path, marker, first, _ = readonly
    repos.write(path, "src/OrderService.cs", "written by someone\n")
    with pytest.raises(WorktreeDirty) as caught:
        worktrees.sync_readonly(git, path, marker=marker)
    assert caught.value.paths == ("src/OrderService.cs",)
    assert git.at(path).head().commit == first
    assert (path / "src" / "OrderService.cs").read_text(encoding="utf-8") == "written by someone\n"


def test_a_locked_readonly_worktree_is_not_switched(readonly):
    _, git, path, marker, _, _ = readonly
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    with pytest.raises(ReadonlyLocked):
        worktrees.sync_readonly(git, path, marker=marker)


def test_a_local_commit_is_switched_to_without_fetching(readonly):
    repos, git, path, marker, _, second = readonly
    repos.git(git.repo, "remote", "set-url", "origin", str(path.parent / "unreachable.git"))
    assert worktrees.sync_readonly(git, path, marker=marker, commit=second).commit == second
    with pytest.raises(NetworkError, match="本地没有 origin/main 的最新提交，需要 fetch"):
        worktrees.sync_readonly(git, path, marker=marker)
    with pytest.raises(NetworkError, match=f"本地没有 {'1' * 40}"):
        worktrees.sync_readonly(git, path, marker=marker, commit="1" * 40)


def test_unknown_commits_are_reported(readonly):
    _, git, path, marker, _, _ = readonly
    with pytest.raises(RefNotFound):
        worktrees.sync_readonly(git, path, marker=marker, commit="0" * 40)


def test_lock_removes_write_permission_and_restore_brings_it_back(readonly, clock):
    _, _, path, marker, _, _ = readonly
    source = path / "src" / "OrderService.cs"
    before = stat.S_IMODE(os.lstat(source).st_mode)
    (path / "link").symlink_to(path.parent)
    worktrees.lock_readonly(path, marker, clock)
    try:
        assert marker.exists()
        with pytest.raises(ReadonlyLocked):
            worktrees.lock_readonly(path, marker, clock)
        assert not os.access(source, os.W_OK) and not os.access(path / "src", os.W_OK)
        with pytest.raises(PermissionError):
            source.write_text("x", encoding="utf-8")
        assert os.access(path.parent, os.W_OK)  # 符号链接不跟随
    finally:
        worktrees.restore_readonly(marker)
    assert stat.S_IMODE(os.lstat(source).st_mode) == before and not marker.exists()


def test_a_failed_lock_is_undone(readonly, clock):
    _, _, path, marker, _, _ = readonly
    calls = []

    def chmod(target, mode):
        calls.append(mode)
        if len(calls) == 3:
            raise PermissionError("denied")
        os.chmod(target, mode, follow_symlinks=False)

    with pytest.raises(PermissionError):
        worktrees.lock_readonly(path, marker, clock, chmod=chmod)
    assert os.access(path / "src", os.W_OK) and not marker.exists()


def test_a_failed_restore_keeps_the_marker(readonly, clock):
    _, _, path, marker, _, _ = readonly
    worktrees.lock_readonly(path, marker, clock)

    def refuse(target, mode):
        raise PermissionError("denied")

    with pytest.raises(ReadonlyRestoreFailed):
        worktrees.restore_readonly(marker, chmod=refuse)
    assert marker.exists()
    worktrees.restore_readonly(marker)


def test_recover_restores_only_locks_of_dead_processes(readonly, clock):
    _, _, path, marker, _, _ = readonly
    worktrees.lock_readonly(path, marker, clock)
    assert worktrees.recover_readonly(marker.parent, alive=lambda pid: True) == []
    assert marker.exists()
    recovered = worktrees.recover_readonly(marker.parent, alive=lambda pid: False)
    assert [item.worktree for item in recovered] == [path]
    assert os.access(path / "src", os.W_OK) and not marker.exists()


def test_cleanup_needs_a_clean_worktree_and_deletes_only_merged_branches(clone, scope, tmp_path):
    repos, _, repo, git = clone
    path = tmp_path / "ws" / "worktrees" / "0007"
    worktrees.create_fix(git, path, "cty/fix-order", scope=scope)
    repos.write(path, "src/OrderService.cs", "未提交\n")
    with pytest.raises(WorktreeDirty):
        worktrees.cleanup(git, path, "cty/fix-order", scope=scope)
    assert path.exists() and git.branch_exists("cty/fix-order")
    repos.commit(path, "fix: order")
    with pytest.raises(CommandFailed, match="branch -d"):
        worktrees.cleanup(git, path, "cty/fix-order", scope=scope)
    assert not path.exists() and git.branch_exists("cty/fix-order")
    repos.git(repo, "merge", "-q", "--no-edit", "cty/fix-order")
    worktrees.cleanup(git, path, "cty/fix-order", scope=scope)
    assert not git.branch_exists("cty/fix-order")
    worktrees.cleanup(git, path, "cty/fix-order", scope=scope)
    removes = [command.argv for command in git.runner.commands if "remove" in command.argv]
    assert removes and all("--force" not in argv for argv in removes)
    assert not any("-D" in command.argv for command in git.runner.commands)
