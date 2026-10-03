"""运行摘要中 Issue 一节(design 4.9)：新建、追加、出错的 Issue，待放行的数量与超时未放行的 Issue。"""

from __future__ import annotations

from collections.abc import Sequence


def summary(run_id: str, created: Sequence[str], appended: Sequence[str], failed: Sequence[str], in_review: int,
            overdue: Sequence[str] = ()) -> str:
    lines = [f"## Issue {run_id}", ""]
    lines += [f"- 新建：{'、'.join(created) or '无'}", f"- 追加：{'、'.join(appended) or '无'}",
              f"- 待放行：{in_review} 个"]
    if overdue:
        lines.append(f"- 待放行超过期限：{'、'.join(overdue)}")
    if failed:
        lines.append(f"- 出错：{'；'.join(failed)}")
    return "\n".join(lines) + "\n"
