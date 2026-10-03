import pytest

from tightrein.store.files.layout import WorkspaceLayout
from tightrein.vcs import worktrees
from tightrein.vcs.errors import NetworkError, RefNotFound, WorktreeDirty, WorktreeLocked
from tightrein.vcs.git_read import GitReader
from tightrein.vcs.process import VcsProcess


@pytest.fixture
def setup(repos, tmp_path):
    origin, repo = repos.origin_and_clone()
    layout = WorkspaceLayout(tmp_path / "ws")
    first = repos.head(repo)
    repos.git(repo, "worktree", "add", "-q", "--detach", str(layout.readonly_worktree()), first)
    second = repos.commit(repo, "feat: second", {"src/New.cs": "class New {}\n"})
    repos.git(repo, "push", "-q", "origin", "main")
    return repos, repo, layout, GitReader(VcsProcess(environ=repos.environ)), first, second


def test_sync_moves_the_detached_head(setup):
    repos, repo, layout, git, first, second = setup
    result = worktrees.sync_readonly(git, layout)
    assert (result.previous, result.commit, result.moved) == (first, second, True)
    assert git.head(layout.readonly_worktree()).commit == second
    again = worktrees.sync_readonly(git, layout, first)
    assert (again.commit, git.head(layout.readonly_worktree()).branch) == (first, None)
    assert not worktrees.sync_readonly(git, layout, first).moved
    assert git.head(repo).branch == "main"


def test_build_products_do_not_block_the_switch(setup):
    repos, _, layout, git, _, second = setup
    repos.write(layout.readonly_worktree(), "bin/Debug/app.dll", "binary")
    assert worktrees.sync_readonly(git, layout).commit == second


def test_a_dirty_readonly_worktree_is_not_switched(setup):
    repos, _, layout, git, first, _ = setup
    repos.write(layout.readonly_worktree(), "src/OrderService.cs", "written by someone\n")
    with pytest.raises(WorktreeDirty) as caught:
        worktrees.sync_readonly(git, layout)
    assert caught.value.paths == ("src/OrderService.cs",)
    assert git.head(layout.readonly_worktree()).commit == first
    written = layout.readonly_worktree() / "src" / "OrderService.cs"
    assert written.read_text(encoding="utf-8") == "written by someone\n"


def test_a_locked_readonly_worktree_is_not_switched(setup):
    _, _, layout, git, _, _ = setup
    marker = layout.readonly_guard("readonly")
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    with pytest.raises(WorktreeLocked):
        worktrees.sync_readonly(git, layout)


def test_unknown_commits_and_lookup(setup):
    _, repo, layout, git, _, _ = setup
    with pytest.raises(RefNotFound):
        worktrees.sync_readonly(git, layout, "0" * 40)
    found = worktrees.find(git, repo, layout.readonly_worktree())
    assert found is not None and found.detached
    assert worktrees.find(git, repo, layout.fix_worktree("0007")) is None


def test_a_local_commit_is_switched_to_without_fetching(setup):
    repos, repo, layout, git, first, second = setup
    repos.git(repo, "remote", "set-url", "origin", str(layout.root / "unreachable.git"))
    git = GitReader(VcsProcess(environ=repos.environ, retry_delays=()))
    assert worktrees.sync_readonly(git, layout, second).commit == second
    with pytest.raises(NetworkError, match="本地没有 origin/main 的最新提交，需要 fetch"):
        worktrees.sync_readonly(git, layout)
    with pytest.raises(NetworkError, match=f"本地没有 {'1' * 40}"):
        worktrees.sync_readonly(git, layout, "1" * 40)
