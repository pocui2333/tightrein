import pytest

from tightrein.domain.enums import ViolationKind
from tightrein.guards import git_state
from tightrein.vcs.git_read import GitReader
from tightrein.vcs.process import VcsProcess


@pytest.fixture
def clone(repos):
    _, repo = repos.origin_and_clone()
    return repos, repo, GitReader(VcsProcess(environ=repos.environ))


def changes(git, repo, action):
    before = git_state.take(git, repo)
    action()
    return [item.kind for item in git_state.compare(before, git_state.take(git, repo), git, repo)]


def test_nothing_changed(clone):
    repos, repo, git = clone
    repos.write(repo, "src/OrderService.cs", "上一轮留下的改动\n")
    assert changes(git, repo, lambda: repos.write(repo, "src/OrderService.cs", "agent 的改动\n")) == []


def test_commit_created(clone):
    repos, repo, git = clone
    before = git_state.take(git, repo)
    old = before.head
    new = repos.commit(repo, "feat: agent commit", {"a.txt": "a"})
    found = git_state.compare(before, git_state.take(git, repo), git, repo)
    assert [item.kind for item in found] == [ViolationKind.GIT_COMMIT_CREATED]
    assert new in found[0].detail and f"git reset --soft {old}" in found[0].detail


def test_branch_switched_and_ref_changed(clone):
    repos, repo, git = clone
    assert changes(git, repo, lambda: repos.git(repo, "checkout", "-q", "-b", "cty/other")) == [
        ViolationKind.GIT_BRANCH_SWITCHED, ViolationKind.GIT_REF_CHANGED,
    ]
    assert changes(git, repo, lambda: repos.git(repo, "tag", "v1")) == [ViolationKind.GIT_REF_CHANGED]


def test_remote_stash_and_worktree_changes(clone):
    repos, repo, git = clone
    moved = changes(git, repo, lambda: repos.git(repo, "remote", "set-url", "origin", "https://example.com/o/r.git"))
    assert moved == [ViolationKind.GIT_REMOTE_CHANGED]

    def stash():
        repos.write(repo, "README.md", "stash me\n")
        repos.git(repo, "stash", "-q")

    assert changes(git, repo, stash) == [ViolationKind.GIT_STASH_CHANGED]
    extra = repo.parent / "worktrees" / "extra"
    assert changes(git, repo, lambda: repos.git(repo, "worktree", "add", "-q", "--detach", str(extra))) == [
        ViolationKind.GIT_WORKTREE_CHANGED,
    ]


def test_merge_in_progress(clone):
    repos, repo, git = clone
    repos.git(repo, "checkout", "-q", "-b", "side")
    repos.commit(repo, "side", {"README.md": "side\n"})
    repos.git(repo, "checkout", "-q", "main")
    repos.commit(repo, "main", {"README.md": "main\n"})

    def merge():
        try:
            repos.git(repo, "merge", "side")
        except AssertionError:
            pass

    kinds = changes(git, repo, merge)
    assert kinds == [ViolationKind.GIT_OPERATION_STARTED]


def test_detached_head(clone):
    repos, repo, git = clone
    readonly = repo.parent / "worktrees" / "readonly"
    repos.git(repo, "worktree", "add", "-q", "--detach", str(readonly))
    first = repos.head(repo)
    repos.commit(repo, "feat: second", {"b.txt": "b"})
    assert changes(git, readonly, lambda: repos.commit(readonly, "agent", {"c.txt": "c"})) == [
        ViolationKind.GIT_COMMIT_CREATED,
    ]
    assert changes(git, readonly, lambda: repos.git(readonly, "checkout", "-q", "--detach", "main")) == [
        ViolationKind.GIT_BRANCH_SWITCHED,
    ]
    assert git_state.take(git, readonly).to_dict()["head"] != first
