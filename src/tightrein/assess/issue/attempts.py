"""修复尝试：Issue 在验收中回归(或完成后关联问题回归、被重开)退回待修后，重新开始一次修复，不沿用上一次的产物。

- 尝试编号记在 issues.extra.attempt(没有即第 1 次)；回归、重开且上一次已动过手(有分支、PR、合并提交或发布进度)时，
  转换(transitions.transition，纯函数)开下一次：编号加一，上一次的分支、PR、合并提交、部署与发布进度
  (extra.release)挪进 extra.attempts 留档，Issue 记录上清空。发布因此不会复用上一次已合并的 PR、部署与撤销记录，
  验收的观察期从这一次的部署算起，不会按上一次的旧出现再判一次回归；
- 上一次实施与发布的文件(交接、日志、检查点、给人看的文档)在这一次开始实施时(待修 → 实施中之前)挪进
  `attempt_<上一次的编号>/`(layout.attempt_dir)：恢复(protocol/recovery.checkpoints)只看对象目录这一层，各步的交接
  就只剩这一次的；文件名照旧按 naming 的规则，归档目录里的 `ls` 仍是那一次的流程顺序。放在开始实施时而不是回归时挪：
  回归那次运行的复盘还要读这一次的交接；先挪再转换，中途中断时下次照样从待修开始、挪动可重复做；
- 留在对象目录的：共用文件(00)、采集与评估的文件(1x、2x)与已有的归档目录。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from tightrein.protocol.naming import Clock, format_iso
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.tables.issues import Issue

ATTEMPT = "attempt"  # extra：当前是第几次尝试
ATTEMPTS = "attempts"  # extra：之前各次尝试的留档
RELEASE = "release"  # extra：发布进度(release/record.EXTRA_KEY)
ACCEPT_UNTIL = "acceptUntil"  # extra：验收观察期截止(release/release.ACCEPT_UNTIL)
_KEEP = re.compile(r"^(?:00|[12]\d)-|^attempt_\d+$")


def current(issue: Issue) -> int:
    return int(issue.extra.get(ATTEMPT) or 1)


def started(issue: Issue) -> bool:
    """这一次尝试已经动过手(建了分支、提了 PR、合并过或进了发布)。"""
    return bool(issue.branch or issue.pr or issue.merge_commit or issue.extra.get(RELEASE))


def begin_next(issue: Issue, clock: Clock, reason: str) -> Issue:
    """开下一次尝试(新对象，不改原记录)：上一次的分支、PR、合并提交、部署与发布进度留档后清空。"""
    extra: dict[str, Any] = dict(issue.extra)
    record = {"attempt": current(issue), "endedAt": format_iso(clock.now()), "reason": reason,
              "branch": issue.branch, "pr": issue.pr, "mergeCommit": issue.merge_commit, "deploy": issue.deploy,
              RELEASE: extra.pop(RELEASE, None)}
    extra.pop(ACCEPT_UNTIL, None)
    extra[ATTEMPTS] = [*(extra.get(ATTEMPTS) or []), record]
    extra[ATTEMPT] = current(issue) + 1
    return replace(issue, branch=None, pr=None, merge_commit=None, deploy=None, gate=None, extra=extra)


def leftovers(layout: WorkspaceLayout, issue: str) -> list[Path]:
    """对象目录中属于上一次尝试的文件与目录(实施、发布的产物与给人看的文档)。"""
    directory = layout.subject_dir(issue)
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if not _KEEP.match(path.name))


def archive(layout: WorkspaceLayout, issue: Issue) -> Path | None:
    """开始这一次实施之前，把上一次尝试留下的文件挪进 `attempt_<上一次>/`；没有可挪的(第 1 次，或已挪过)返回 None。
    同名的已在归档里(上次挪到一半中断)时覆盖，可重复做。"""
    found = leftovers(layout, issue.id)
    if current(issue) < 2 or not found:
        return None
    target = layout.attempt_dir(issue.id, current(issue) - 1)
    target.mkdir(parents=True, exist_ok=True)
    for path in found:
        destination = target / path.name
        if destination.is_dir():
            shutil.rmtree(destination)
        elif destination.exists():
            destination.unlink()
        shutil.move(str(path), str(destination))
    return target
