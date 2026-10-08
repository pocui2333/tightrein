"""提交：把实施交付的改动提交到修复分支。

- 提交前要求最后一轮确定性检查全部通过；用户接受的未通过项照常提交，提交时写进 Issue 历史(一次)，并写进 PR 描述、
  使自动合并不成立(merge.py)；
- 提交的文件清单取工作区的实际改动，与交付的 changedFiles 不一致就停：说明哪些是交付之后新增或消失的，防止把无关文件
  或临时文件一起提交；
- 提交信息完全由程序按项目约定(protocol/git/format)拼出：第一行用 agent 给的一句话，空一行写原因；不带任何署名行；
- 幂等键取改动的 diff_hash(与是否已提交无关)，中断后重来不会重复提交；
- 做决定时(核对完清单)记下分支、HEAD 与改动哈希(Git.state)，作为 expected 交给提交：执行前任何一项变了即抛 Stale、
  不提交(由 release.py 停到下次重新观察)。
"""

from __future__ import annotations

from tightrein.assess.issue import files
from tightrein.assess.issue.transitions import HISTORY
from tightrein.protocol.git import Conventions, Git
from tightrein.protocol.git.format import commit_message
from tightrein.protocol.runtime import Runtime
from tightrein.release.record import POINT_PR, Delivery, ReleaseBlocked, now_iso
from tightrein.store.tables.issues import Issue

ACTOR = "release"
DECISION_KEYS = ("branch", "head", "diffHash")  # 提交时复核的前置条件


def commit(runtime: Runtime, issue: Issue, delivery: Delivery, conventions: Conventions, worktree: Git) -> str | None:
    """提交工作区的改动，返回新的 HEAD；工作区已经干净(提交过了)时为 None。"""
    status = worktree.status()
    if status.clean:
        return None
    if not delivery.checks_passed and not delivery.accepted:
        failed = "、".join(check.name for check in delivery.checks if not check.passed)
        raise ReleaseBlocked(POINT_PR, f"最后一轮检查没有全部通过：{failed}；先回到实施处理，或接受这些未通过项后再发布")
    # 实施中途的检查点可能已提交了一部分，所以按基准到工作区的全部改动核对，只提交还没提交的
    base = worktree.review_base(f"origin/{worktree.main_branch}")
    differences = file_differences(worktree.changed_files(base), delivery.changed_files)
    if differences:
        raise ReleaseBlocked(POINT_PR, "交付之后工作区有变化：" + "；".join(differences) + "；先回到实施重新自检与审查")
    message = commit_message(conventions.commit, kind=conventions.types[issue.kind], scope=delivery.text.scope,
                             summary=delivery.text.summary or issue.title, why=delivery.text.why)
    decided = worktree.state(DECISION_KEYS, base=base)
    head = worktree.commit(status.changed_paths, message, base=base, scope=runtime.scope(issue.id, POINT_PR),
                           expected=decided)
    if delivery.accepted:
        record_accepted(runtime, issue, delivery.accepted)
    return head


def record_accepted(runtime: Runtime, issue: Issue, accepted: tuple[str, ...]) -> None:
    """用户接受的未通过项写进 Issue 历史(改的是传入的记录，之后保存发布状态时一并带上)。"""
    issue.extra.setdefault(HISTORY, []).append({
        "at": now_iso(runtime), "event": "commit", "actor": ACTOR, "reason": None,
        "note": "带着用户接受的未通过项提交：" + "；".join(accepted)})
    files.write(runtime, issue)


def file_differences(actual: tuple[str, ...], delivered: tuple[str, ...]) -> list[str]:
    """工作区实际改动与交付清单的差异，按文件名排序。"""
    now, before = set(actual), set(delivered)
    found = [f"交付之后新增的改动：{path}" for path in sorted(now - before)]
    return found + [f"交付时改动过、现在没有改动：{path}" for path in sorted(before - now)]
