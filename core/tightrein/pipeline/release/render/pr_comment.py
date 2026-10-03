"""PR 上的两种文字(architecture/07 19.7，redesign/07-release.md 第 2 节)：

- render：部署后确认通过的回复草稿 data/fixes/<编号>/pr-comment.md，由用户确认内容后自行回复到 PR；
- review：修复最后一轮 AI 评审的结论，由 release 以评论发到 PR(release.reviewComment)，只作说明，不作批准。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

RESULTS = {"pass": "通过", "weak": "弱证据", "unverified": "未验证", "fail": "失败"}
REVIEW_NOTE = "以下是 tightrein 的 AI 评审结论，只作说明，不是批准；是否合并由维护者决定。"


def render(staging: Mapping[str, Any]) -> str:
    lines = [f"已在测试环境验证通过(部署 commit {staging['commit'][:12]})。", ""]
    lines += [f"- {item['id']}：{RESULTS[item['result']]}" for item in staging["items"]]
    return "\n".join(lines) + "\n"


def review(last: Mapping[str, Any], conclusions: Sequence[str]) -> str:
    """最后一轮评审：各评审的结论、确定性检查是否全部通过、阻断项。"""
    lines = [REVIEW_NOTE, "", f"第 {last['round']} 轮评审："]
    lines += [f"- {text}" for text in conclusions] or ["- 本轮没有运行评审(微档且检查都通过时跳过)"]
    lines.append(f"- 确定性检查：{'全部通过' if last['checksPassed'] else '有未通过项'}")
    failures = last.get("failures") or []
    lines.append(f"- 阻断项：{len(failures)} 项" + ("" if not failures else "：" + "；".join(
        f"{item['check']} {item.get('location') or ''} {item['problem']}".replace("  ", " ") for item in failures)))
    return "\n".join(lines) + "\n"
