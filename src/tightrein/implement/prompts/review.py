"""审查(implement.review、implement.review.deep)的提示变量。

- 轻量审查：Issue、验收标准、代码笔记、方案摘要(根因假说只留因果链与修改位置)、改动、自检的实际结果与疑似写死提示；
  修正轮次另给上一轮的问题，改动只给这一轮又改了的文件；
- 深度审查是盲审：只给验收标准、最终改动与自检的实际输出，不给 Issue 正文、方案、根因假说与编码模型的任何说明，
  让它独立于编码时的思路判断。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from tightrein.implement.prompts.common import (
    NONE,
    acceptance,
    issue_text,
    json_text,
    list_text,
    notes_text,
    plan_view,
)
from tightrein.protocol.security import external

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext, Risk

PLAN_FOR_REVIEW = ("summary", "hypothesis", "files", "acceptanceMapping", "notDoing", "migration")
DIFF_LIMIT = 400_000  # 改动量已有上限，这里只防意外的超大补丁
FULL_SCOPE = "本轮审查全部改动。"
INCREMENTAL_SCOPE = ("修正轮次：下面只给这一轮又改了的文件({files})。先逐条核对上一轮的问题是否已解决，没解决的照常列为"
                     "阻断项；再看这一轮的改动有没有引入新问题。")


def light(context: ImplementContext, *, plan: Mapping[str, Any], diff: str, results: str, hints: Sequence[str],
          previous: Sequence[str], files: Sequence[str] | None) -> dict[str, str]:
    """files 为 None 时审全部改动；否则为修正轮次这一轮又改了的文件。"""
    return {
        "issue": issue_text(context),
        "acceptance": list_text(acceptance(context.body)),
        "notes": notes_text(context),
        "plan": json_text(plan_view(plan, PLAN_FOR_REVIEW)) if plan else NONE,
        "scope": FULL_SCOPE if files is None else INCREMENTAL_SCOPE.format(files="、".join(files) or NONE),
        "previous": list_text(previous),
        "diff": diff_text(diff),
        "results": results or NONE,
        "hints": list_text(hints),
    }


def deep(context: ImplementContext, *, diff: str, results: str, risk: Risk | None) -> dict[str, str]:
    reasons = list(risk.reasons) if risk is not None else []
    return {
        "acceptance": list_text(acceptance(context.body)),
        "risk": list_text(reasons),
        "diff": diff_text(diff),
        "results": results or NONE,
    }


def diff_text(diff: str) -> str:
    """被改的是项目代码，注释与字符串里可能有冒充指令的文字：当作数据包住。"""
    return external("diff", diff or "(没有改动)", limit=DIFF_LIMIT)


def results_text(check: Mapping[str, Any]) -> str:
    """自检的实际结果：命令与结论、精简后的失败输出、本机运行检查、改动统计。"""
    counted = check.get("counted") or {}
    lines = [f"- 改动(不含测试与生成文件)：{counted.get('files', 0)} 个文件、{counted.get('lines', 0)} 行"]
    for item in check.get("commands") or []:
        lines.append(f"- `{item['command']}`：{item['result']}" + (f"({item['reason']})" if item.get("reason") else ""))
        if item.get("output"):
            lines.append(f"```\n{item['output']}\n```")
    for item in check.get("runtime") or []:
        lines.append(f"- {item['id']}：{item['result']}" + (f"({item['reason']})" if item.get("reason") else ""))
    return "\n".join(lines)
