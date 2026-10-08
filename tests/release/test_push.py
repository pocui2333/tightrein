"""推送：origin/main 有新提交时先同步不推送；待推送的提交以 origin/<分支>(首次为 origin/main)为基准列出；
没有要推送的不推。"""

import pytest

from tightrein.protocol.git import Stale
from tightrein.release.push import DECISION_KEYS, push
from tightrein.release.record import ReleaseBlocked


def test_the_first_push_lists_commits_since_main(kit, runtime, repos, worktree):
    commit = repos.commit(worktree, "fix: page", {"src/order.py": "PAGE = 0\n"})
    result = push(runtime, "0007", kit.BRANCH, runtime.git.at(worktree))
    assert result.commit == commit
    assert [line.split(" ", 1)[1] for line in result.commits] == ["fix: page"]
    assert repos.git(repos.origin, "rev-parse", f"refs/heads/{kit.BRANCH}").strip() == commit


def test_only_new_commits_are_pushed(kit, runtime, repos, worktree):
    repos.commit(worktree, "fix: page", {"src/order.py": "PAGE = 0\n"})
    git = runtime.git.at(worktree)
    push(runtime, "0007", kit.BRANCH, git)
    assert push(runtime, "0007", kit.BRANCH, git).commit is None
    repos.commit(worktree, "test: page", {"tests/test_order.py": "def test(): pass\n"})
    again = push(runtime, "0007", kit.BRANCH, git)
    assert [line.split(" ", 1)[1] for line in again.commits] == ["test: page"]


def test_main_moving_first_needs_a_sync(kit, runtime, repos, worktree):
    repos.commit(worktree, "fix: page", {"src/order.py": "PAGE = 0\n"})
    repos.upstream("feat: user", {"src/user.py": "NAME = 'b'\n"})
    with pytest.raises(ReleaseBlocked, match="先同步 main"):
        push(runtime, "0007", kit.BRANCH, runtime.git.at(worktree))


def test_the_push_rechecks_the_state_seen_when_deciding(kit, runtime, repos, worktree, monkeypatch):
    repos.commit(worktree, "fix: page", {"src/order.py": "PAGE = 0\n"})
    git = runtime.git.at(worktree)
    seen = kit.stale_at_decision(monkeypatch, git)
    with pytest.raises(Stale, match="head"):
        push(runtime, "0007", kit.BRANCH, git)
    assert set(seen[0]) == set(DECISION_KEYS) == {"branch", "head", "originMain"}
    assert repos.git(repos.origin, "branch", "--list", kit.BRANCH).strip() == ""  # 没有推送
    assert push(runtime, "0007", kit.BRANCH, git).commit == repos.head(worktree)
