"""截图查看(architecture/07 13.2)：调用 fix-reviewer 的截图评审；「有问题」计为失败项，「无法判断」或执行器无法完成
(例如所用工具不能读取图片)时交用户查看，用户用 verify screenshots --ok 或 --issue <说明> 给出结论。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from tightrein.domain.enums import CheckResult, RunnerStatus
from tightrein.pipeline.verify.steps.regression import Screenshot
from tightrein.pipeline.verify.steps.verdict import SCREENSHOT, Item
from tightrein.runner.result import RunnerResult
from tightrein.runner.task import RunnerTask

RESULTS = {"ok": CheckResult.PASS, "issue": CheckResult.FAIL}
NO_SCREENSHOTS = "巡检没有产出受影响页面的截图"


@dataclass
class ScreenshotResult:
    items: list[Item] = field(default_factory=list)
    awaiting: bool = False


def review(run: Callable[[RunnerTask], RunnerResult], build: Callable[[Sequence[tuple[str, str]]], RunnerTask],
           screenshots: Sequence[Screenshot]) -> ScreenshotResult:
    if not screenshots:
        return ScreenshotResult([Item("screenshots", SCREENSHOT, CheckResult.UNVERIFIED, reason=NO_SCREENSHOTS)])
    result = run(build([(item.path, item.note) for item in screenshots]))
    if result.status is not RunnerStatus.OK or result.output is None:
        reason = f"截图评审没有完成({result.status.value})，请用户查看"
        return ScreenshotResult([Item(f"screenshot:{item.path}", SCREENSHOT, CheckResult.UNVERIFIED, None, (item.path,),
                                      reason) for item in screenshots], True)
    found = ScreenshotResult()
    for entry in result.output.get("screenshots") or []:
        verdict = RESULTS.get(entry["result"], CheckResult.UNVERIFIED)
        found.awaiting = found.awaiting or entry["result"] == "unknown"
        found.items.append(Item(f"screenshot:{entry['path']}", SCREENSHOT, verdict, None, (entry["path"],),
                                entry["reason"]))
    return found


def user_verdict(items: Sequence[Item], ok: bool, note: str | None) -> list[Item]:
    """用户给出的结论替换待查看的截图项；--issue 计为失败。"""
    result = CheckResult.PASS if ok else CheckResult.FAIL
    reason = "用户查看截图：无问题" if ok else f"用户查看截图：{note}"
    return [Item(item.id, item.category, result, item.command, item.evidence, reason)
            if item.category == SCREENSHOT and item.result is not CheckResult.FAIL else item for item in items]
