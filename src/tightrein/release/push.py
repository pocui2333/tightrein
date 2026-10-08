"""推送：把修复分支推到 origin。

- 推送前 fetch：origin/main 有新提交就先同步(sync.py)，不推送；
- 待推送的提交以 origin/<分支>(首次推送时 origin/main)为基准列出，没有就不推；
- 结果按 `git push --porcelain` 的引用标记判断(protocol/git)，被拒时提示先同步主干，绝不强推；
- 做决定时(fetch 并确认 main 没动之后)记下分支、HEAD 与 origin/main(Git.state)，作为 expected 交给推送：执行前
  任何一项变了即抛 Stale、不推送(由 release.py 停到下次重新观察)；
- 推送的 commit 记为「本工具最近一次推送的 commit」：PR 头部不等于它说明 PR 上有别人的提交，不自动合并。
"""

from __future__ import annotations

from dataclasses import dataclass

from tightrein.protocol.git import Git, PushRejected
from tightrein.protocol.runtime import Runtime
from tightrein.release.record import POINT_PR, ReleaseBlocked

DECISION_KEYS = ("branch", "head", "originMain")  # 推送时复核的前置条件


@dataclass(frozen=True)
class PushResult:
    commit: str | None  # 推送的 commit；没有要推送的提交时为 None
    commits: tuple[str, ...]  # 这次推送的提交(短哈希 + 标题)


def push(runtime: Runtime, issue: str, branch: str, worktree: Git) -> PushResult:
    worktree.fetch()
    main_ref = f"origin/{worktree.main_branch}"
    if worktree.is_ancestor(main_ref, "HEAD") is False:
        raise ReleaseBlocked(POINT_PR, f"{main_ref} 有新提交，先同步 main 再推送")
    remote = f"origin/{branch}"
    base = remote if worktree.has_commit(remote) else main_ref
    commits = tuple(f"{item.commit[:12]} {item.subject}" for item in worktree.log(f"{base}..HEAD"))
    if not commits:
        return PushResult(None, ())
    decided = worktree.state(DECISION_KEYS)
    try:
        pushed = worktree.push(scope=runtime.scope(issue, POINT_PR), expected=decided)
    except PushRejected as error:
        raise ReleaseBlocked(POINT_PR, f"远程拒绝推送 {'、'.join(error.rejected)}：先同步 main 再发布；不强推") from error
    return PushResult(pushed, commits)
