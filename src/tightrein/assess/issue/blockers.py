"""前置 Issue(extra.blockedBy)：一个 Issue 要等别的 Issue 先合进主干才能做。

- 谁记下：方案(implement.design)发现要用到的代码要由别的 Issue 先合入时返回 blockedBy，实施经状态机 BLOCK 退回待修
  并记下；拆分(create.split_back)出的后一个 Issue 记下前一个；
- 谁看：调度挑 Issue 时跳过还有前置没落地的(protocol/schedule `_issues_to_implement`)；实施推导下一步时，上一版方案
  在等的前置都落地了就从准备重新做起(implement.py)，worktree 快进到含前置改动的最新主干；
- 落地：已合并(验收观察中、已完成)。取消的不会再合并，也不再挡着；不存在的编号同样不挡着。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping

from tightrein.assess.issue.transitions import BLOCKED_BY, IssueStatus
from tightrein.store.tables import issues
from tightrein.store.tables.issues import Issue

LANDED = frozenset({IssueStatus.ACCEPTING, IssueStatus.DONE})  # 改动已在主干上
SETTLED = LANDED | {IssueStatus.CANCELLED}  # 不再挡着别的 Issue


def of(issue: Issue) -> tuple[str, ...]:
    """Issue 记下的前置 Issue。"""
    return tuple(str(item) for item in issue.extra.get(BLOCKED_BY) or [])


def statuses(conn: sqlite3.Connection) -> dict[str, str]:
    """全部 Issue 的状态(编号 → 状态)：一次查出，判断多个 Issue 的前置时共用。"""
    return {item.id: item.status for item in issues.find(conn)}


def waiting(blockers: Iterable[str], known: Mapping[str, str]) -> list[str]:
    """blockers 中还没落地的(known 为 statuses 的结果)。"""
    return [item for item in blockers if item in known and known[item] not in SETTLED]
