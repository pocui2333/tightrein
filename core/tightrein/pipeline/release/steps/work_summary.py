"""工作总结 data/fixes/<编号>/summary.md(architecture/07 19.9)：一个标题加不超过 thresholds.release.summaryItems 条，
只写功能不写实现，内容取自 Issue「结论」、userVisibleChange 与验证结论；附合并前验证中的截图路径。没有截图时：
合并前验证的页面类检查没有通过(未执行或未验证，例如端口被占、启动失败)或没有合并前验证的结果，写明界面变化未确认
及原因；否则写「无界面变化」。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tightrein.domain.enums import CheckResult
from tightrein.pipeline.verify.steps.verdict import PAGE_PATROL

NO_UI = "无界面变化"
NO_LOCAL = "没有合并前验证的结果"
UNCONFIRMED = "页面验证未执行({reason})，界面变化未确认"


def page_note(local: Mapping[str, Any] | None) -> str | None:
    """没有截图时代替「无界面变化」的说明；页面类检查都通过或没有页面类检查时为空。"""
    if local is None:
        return UNCONFIRMED.format(reason=NO_LOCAL)
    reasons = [item.get("reason") or item["result"] for item in local.get("items", [])
               if item["category"] == PAGE_PATROL and item["result"] != CheckResult.PASS.value]
    return UNCONFIRMED.format(reason="；".join(reasons)) if reasons else None


def summary(title: str, conclusion: str, fix: Mapping[str, Any], verifications: Sequence[str],
            screenshots: Sequence[str], items: int, count: int, unconfirmed: str | None = None) -> str:
    """unconfirmed 为页面验证没有完成时的说明(page_note)，没有截图时代替「无界面变化」。"""
    lines = [conclusion.strip().splitlines()[0] if conclusion.strip() else title]
    change = (fix.get("userVisibleChange") or "").strip()
    if change and change != "无":
        lines.append(f"用户可见的变化：{change}")
    lines += list(verifications)
    shots = list(screenshots)[:count]
    body = [f"# {title}", "", *(f"- {line}" for line in lines[:items]), ""]
    body += ["截图：", *(f"- {path}" for path in shots)] if shots else [unconfirmed or NO_UI]
    return "\n".join(body) + "\n"
