"""回归时提撤销 PR：先提撤销，再把 Issue 退回待修。

- 撤销在单独的临时 worktree(`revert-<编号>`)与新分支上做，从 fetch 后的 origin/main 建，不碰修复 worktree；
- `git revert` 合并提交(以 merge 方式合并的有两个父提交，加 `-m 1` 取主干一侧)，推送并开 PR，但不合并，交人决定；
- 按「Issue + 合并提交」幂等：同一回归只提一次；又一次修复尝试回归时撤销分支带上次数(建 worktree、撤销、推送、开 PR 各自经 protocol/git 的幂等键)；
- PR 开好后删除临时 worktree(干净才删)，撤销分支留在远程等人处理。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from tightrein.assess.issue import attempts
from tightrein.protocol.git import Conventions, Git, GitHub, WriteScope
from tightrein.protocol.git.format import HOTFIX, BranchRejected, branch_name, kebab
from tightrein.protocol.git.git import MergeConflict
from tightrein.protocol.git.worktrees import create_fix
from tightrein.protocol.runtime import Runtime
from tightrein.release.record import POINT_ACCEPT, ReleaseBlocked
from tightrein.store.tables.issues import Issue

WORKTREE_PREFIX = "revert-"
SLUG_PREFIX = "revert-"


@dataclass(frozen=True)
class Reverted:
    branch: str
    number: int
    url: str


def revert(runtime: Runtime, issue: Issue, merge_commit: str, reason: str, conventions: Conventions,
           github: GitHub) -> Reverted:
    scope = runtime.scope(issue.id, POINT_ACCEPT)
    branch = revert_branch(issue, conventions)
    path = runtime.workspace.worktree(f"{WORKTREE_PREFIX}{issue.id}")
    create_fix(runtime.git, path, branch, scope=scope)
    worktree = runtime.git.at(path)
    revert_commit(worktree, merge_commit, scope=scope)
    worktree.push(scope=scope)
    title = f"Revert: {issue.title}"
    body = "\n".join([f"撤销 Issue {issue.id} 的合并提交 {merge_commit[:12]}。", "", f"原因：{reason}", "",
                      "这个 PR 由 tightrein 在部署后确认发现回归时生成，不会自动合并，请决定是否合并。"]) + "\n"
    number = github.create_pr(branch, runtime.git.main_branch, title, body, scope=scope)
    runtime.git.worktree_remove(path, scope=scope)
    return Reverted(branch, number, github.pr_view(number).url)


def revert_branch(issue: Issue, conventions: Conventions) -> str:
    """撤销分支；又一次修复尝试(assess/issue/attempts)回归时带上次数，不与上一次的撤销分支同名。"""
    number = attempts.current(issue)
    slug = f"{SLUG_PREFIX}{kebab(issue.title)}" + (f"-{number}" if number > 1 else "")
    try:
        return branch_name(conventions, kind=HOTFIX, issue=issue.id, slug=slug)
    except BranchRejected:
        # 项目要求个人前缀而本机没配：撤销分支不带前缀，PR 里写明它由 tightrein 生成
        return branch_name(_unprefixed(conventions), kind=HOTFIX, issue=issue.id, slug=slug)


def revert_commit(worktree: Git, commit: str, *, scope: WriteScope) -> str:
    """在 worktree 的当前分支上撤销 commit，返回新的 HEAD；冲突时停下交人(不自行取舍)。"""
    try:
        return worktree.revert(commit, scope=scope)
    except MergeConflict as error:
        raise ReleaseBlocked(POINT_ACCEPT, f"撤销 {commit[:12]} 出现冲突：{'、'.join(error.files)}；"
                             f"在 {worktree.repo} 手动处理") from error


def _unprefixed(conventions: Conventions) -> Conventions:
    return replace(conventions, personal_prefix=False, branch=conventions.branch.replace("{prefix}", ""))
