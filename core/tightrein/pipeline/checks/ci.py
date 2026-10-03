"""项目 CI 必需检查的状态(redesign/06-verify.md 第 1 节 PR 阶段)，纯函数。

输入为 `gh pr checks <编号> --required --json name,state,bucket` 的结果(--json 时退出码总是 0，按 bucket 判断：
pass、fail、pending、skipping、cancel)。没有必需检查(主分支没有分支保护或规则集的必需检查)时为 skipped。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

SKIPPED = "skipped"
PENDING = "pending"
PASSED = "passed"
FAILED = "failed"
FAILED_BUCKETS = frozenset({"fail", "cancel"})
PENDING_BUCKETS = frozenset({"pending"})


@dataclass(frozen=True)
class CiState:
    state: str
    failed: tuple[str, ...] = ()
    pending: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "failed": list(self.failed), "pending": list(self.pending)}

    @property
    def reason(self) -> str | None:
        """不满足自动合并条件时的原因；通过、跳过与进行中(GitHub 原生自动合并会等待)时为空。"""
        return f"CI 必需检查未通过：{'、'.join(self.failed)}" if self.state == FAILED else None


def judge(checks: Sequence[Mapping[str, Any]] | None) -> CiState:
    """checks 为空(None)表示项目没有必需检查。"""
    if checks is None:
        return CiState(SKIPPED)
    failed = tuple(str(item.get("name")) for item in checks if item.get("bucket") in FAILED_BUCKETS)
    pending = tuple(str(item.get("name")) for item in checks if item.get("bucket") in PENDING_BUCKETS)
    if failed:
        return CiState(FAILED, failed, pending)
    if pending or not checks:
        return CiState(PENDING, (), pending)
    return CiState(PASSED)
