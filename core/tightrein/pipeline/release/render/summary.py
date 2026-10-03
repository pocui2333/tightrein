"""运行摘要中 release 一节(architecture/07 7.11)：等待确认的 git 操作、等待审核的 PR、状态变化、部署失败与提醒。"""

from __future__ import annotations

from collections.abc import Sequence


def render(lines: Sequence[str], skipped: Sequence[tuple[str, str]]) -> str:
    parts = ["## release", "", *(f"- {line}" for line in lines)] if lines else ["## release", "", "- 没有变化"]
    if skipped:
        parts += ["", "跳过：", *(f"- Issue {issue_id}：{reason}" for issue_id, reason in skipped)]
    return "\n".join(parts) + "\n"
