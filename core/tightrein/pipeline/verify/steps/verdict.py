"""各检查项的结果与整体结论(redesign/06-verify.md)，纯函数。

单项：验证通过(执行了且满足通过条件，保存了证据)、弱证据(执行了，但前置条件不满足或用例因数据缺失被跳过)、
未验证(没有执行，写明本应验证的行为与原因)、失败(执行了但不满足通过条件)。
PR 阶段：有失败项时为失败；迁移待确认、截图待查看时为等待用户；否则为通过(未验证与弱证据写进报告，不阻断)。
分类中的 project-check、deferred、full-regression 只出现在旧的交接文档中。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tightrein.domain.enums import CheckResult, RegressionResult
from tightrein.pipeline.checks.regressions.runner import RegressionOutcome

ISSUE_REPRO = "issue-repro"
OTHER_REPRO = "other-repro"
API_SHALLOW = "api-shallow"
PAGE_PATROL = "page-patrol"
SCREENSHOT = "screenshot"
DEPLOY_CONFIRM = "deploy-confirm"
PASSED = "passed"
FAILED = "failed"
AWAITING = "awaiting-user"


@dataclass(frozen=True)
class Item:
    id: str
    category: str
    result: CheckResult
    command: str | None = None
    evidence: tuple[str, ...] = ()
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "category": self.category, "command": self.command, "result": self.result.value,
                "evidence": list(self.evidence), "reason": self.reason}


def from_outcome(outcome: RegressionOutcome) -> CheckResult:
    """复现检查应当通过时(修复后、回归)的单项结果。"""
    if outcome.result in (RegressionResult.NOT_RUN, RegressionResult.INVALID):
        return CheckResult.UNVERIFIED
    if not outcome.precondition_met:
        return CheckResult.WEAK
    return CheckResult.PASS if outcome.result is RegressionResult.PASSED else CheckResult.FAIL


def local_conclusion(items: Sequence[Item], awaiting: bool) -> str:
    if any(item.result is CheckResult.FAIL for item in items):
        return FAILED
    return AWAITING if awaiting else PASSED


def first_failure(items: Sequence[Item]) -> Item | None:
    return next((item for item in items if item.result is CheckResult.FAIL), None)
