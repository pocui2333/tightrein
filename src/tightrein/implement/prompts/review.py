"""审查(implement.review、implement.review.deep)的提示变量。

- 轻量审查：Issue、验收标准、代码笔记、方案摘要(根因假说只留因果链与修改位置)、用户的决定与补充、改动、自检的实际
  结果与疑似写死提示；修正轮次另给上一轮的问题，改动只给这一轮又改了的文件；
- 深度审查是盲审：只给验收标准、用户的决定与补充、合并进来的 main、最终改动与自检的实际输出，不给 Issue 正文、方案、
  根因假说与编码模型的任何说明，让它独立于编码时的思路判断；用户的决定是用户本人的要求(高于方案)，两种审查都核对它
  照做了没有；
- 这次交付合并过 main(发布合并 main 后退回实施)时两种审查都拿到合并信息(MergedMain)：两侧都改了的文件、main 一侧
  的相关提交与 main 一侧对这些文件的改动。改动相对合并进来的 main 算，解决冲突时丢掉的 main 一侧在改动里只是一处
  删除，没有这份信息审查分不出它与方案内的删改。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tightrein.implement.prompts.common import (
    NONE,
    acceptance,
    decisions_text,
    issue_text,
    json_text,
    list_text,
    notes_text,
    plan_view,
)
from tightrein.protocol.security import external

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext, Risk

# 方案交给用户拍板的点(userDecisions)也给：用户的决定只写了回答，没有问题本身审查核对不了它照做了没有
PLAN_FOR_REVIEW = ("summary", "hypothesis", "files", "acceptanceMapping", "notDoing", "migration", "userDecisions")
DIFF_LIMIT = 400_000  # 改动量已有上限，这里只防意外的超大补丁
MAIN_DIFF_LIMIT = 100_000  # main 一侧的改动只取两侧都改了的文件，这里只防意外的超大补丁
FULL_SCOPE = "本轮审查全部改动。"
INCREMENTAL_SCOPE = ("修正轮次：下面只给这一轮又改了的文件({files})。先逐条核对上一轮的问题是否已解决，没解决的照常列为"
                     "阻断项；再看这一轮的改动有没有引入新问题。")


@dataclass(frozen=True)
class MergedMain:
    """这次交付合并进来的 main：两侧都改了的文件里，main 一侧的改动必须保留。"""

    main: str  # 合并进来的 main commit
    files: tuple[str, ...]  # 两侧都改了的文件(编码解决过冲突的，或 main 改到修复文件的)
    commits: tuple[str, ...]  # main 一侧改到这些文件的提交(「短哈希 标题」)
    diff: str  # main 一侧对这些文件的改动：从合并前修复分支与 main 的分叉点到 main
    resolved_by_code: bool  # 冲突由编码这一圈解决(否则由发布合并：git 自动合并或人解决)


def light(context: ImplementContext, *, plan: Mapping[str, Any], diff: str, results: str, hints: Sequence[str],
          previous: Sequence[str], files: Sequence[str] | None, merge: MergedMain | None = None) -> dict[str, str]:
    """files 为 None 时审全部改动；否则为修正轮次这一轮又改了的文件。merge 为这次交付合并进来的 main。"""
    return {
        "issue": issue_text(context),
        "acceptance": list_text(acceptance(context.body)),
        "notes": notes_text(context),
        "plan": json_text(plan_view(plan, PLAN_FOR_REVIEW)) if plan else NONE,
        "decisions": decisions_text(context.decisions),
        "merge": merge_text(merge),
        "scope": FULL_SCOPE if files is None else INCREMENTAL_SCOPE.format(files="、".join(files) or NONE),
        "previous": list_text(previous),
        "diff": diff_text(diff),
        "results": results or NONE,
        "hints": list_text(hints),
    }


def deep(context: ImplementContext, *, diff: str, results: str, risk: Risk | None,
         merge: MergedMain | None = None) -> dict[str, str]:
    reasons = list(risk.reasons) if risk is not None else []
    return {
        "acceptance": list_text(acceptance(context.body)),
        "decisions": decisions_text(context.decisions),
        "merge": merge_text(merge),
        "risk": list_text(reasons),
        "diff": diff_text(diff),
        "results": results or NONE,
    }


def diff_text(diff: str) -> str:
    """被改的是项目代码，注释与字符串里可能有冒充指令的文字：当作数据包住。"""
    return external("diff", diff or "(没有改动)", limit=DIFF_LIMIT)


def merge_text(merge: MergedMain | None) -> str:
    """合并进来的 main：两侧都改了的文件、main 一侧的提交与改动(项目代码，当作数据包住)；没有合并时写「无」。"""
    if merge is None:
        return NONE
    how = "合并时冲突，由编码解决" if merge.resolved_by_code else "发布合并时 git 自动合并或由人解决冲突"
    return "\n".join([
        f"- 合并进来的 main：{merge.main[:12]}",
        f"- 两侧都改了的文件({how})：{'、'.join(merge.files)}",
        f"- main 一侧改到这些文件的提交：{'；'.join(merge.commits) or NONE}",
        "- main 一侧对这些文件的改动(从合并前修复分支与 main 的分叉点到合并进来的 main)：",
        "",
        external("main 一侧的改动", merge.diff or "(没有改动)", limit=MAIN_DIFF_LIMIT),
    ])


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
