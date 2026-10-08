"""回归时提撤销 PR：在单独的临时 worktree 与新分支上从 origin/main 撤销合并提交(两个父提交时加 -m 1)，推送并开
PR，不合并；临时 worktree 随后删除，修复 worktree 不动。同一回归只提一次由发布进度(extra.release.revert)保证，
见 test_release.py。"""

from tightrein.protocol.git import resolve
from tightrein.release.accept.revert import revert, revert_branch


def _merged_fix(repos):
    """在 origin/main 上以 merge --no-ff 合进一个修复，返回合并提交。"""
    repos.git(repos.other, "pull", "-q", "origin", "main")
    repos.git(repos.other, "checkout", "-q", "-b", "fix/7-order")
    repos.commit(repos.other, "fix: page", {"src/order.py": "PAGE = 0\n"})
    repos.git(repos.other, "checkout", "-q", "main")
    repos.git(repos.other, "merge", "-q", "--no-ff", "-m", "Merge fix", "fix/7-order")
    repos.git(repos.other, "push", "-q", "origin", "main")
    return repos.head(repos.other)


def test_a_revert_pr_is_opened_but_not_merged(kit, github_runtime, fake_github, repos, worktree):
    merge_commit = _merged_fix(repos)
    issue = kit.new_issue(github_runtime, status="accepting")
    conventions = resolve(github_runtime.settings, repos.repo)
    reverted = revert(github_runtime, issue, merge_commit, "P-0001 部署后再次出现", conventions, fake_github)
    branch = revert_branch(issue, conventions)
    assert reverted.branch == branch and branch.startswith("hotfix/7-revert")
    pushed = repos.git(repos.origin, "show", f"refs/heads/{branch}:src/order.py")
    assert pushed == kit.ORDER  # 撤销后回到合并前的内容
    message = repos.git(repos.origin, "log", "-1", "--format=%s", f"refs/heads/{branch}")
    assert message.startswith("Revert")
    assert fake_github.pulls[reverted.number]["state"] == "OPEN"
    assert "不会自动合并" in fake_github.pulls[reverted.number]["body"]
    assert not any(call[0] == "pr-merge" for call in fake_github.calls)
    assert not github_runtime.workspace.worktree("revert-0007").exists()
    assert worktree.exists()  # 修复 worktree 不动


def test_a_later_attempt_reverts_on_its_own_branch(kit, github_runtime, repos):
    """又一次修复尝试再回归时，撤销分支带上次数，不与上一次的撤销分支同名。"""
    conventions = resolve(github_runtime.settings, repos.repo)
    issue = kit.new_issue(github_runtime, status="accepting")
    first = revert_branch(issue, conventions)
    issue.extra = {**issue.extra, "attempt": 2}
    assert revert_branch(issue, conventions) == f"{first}-2"
