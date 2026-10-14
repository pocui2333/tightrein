"""方案(implement.design)的提示变量。

其他未完成的 Issue 列给方案：本 Issue 要用到的代码要由其中某几个先合入时，方案据此写出前置 Issue(blockedBy)，不问用户。
Issue 正文多用 GitHub 编号互相引用(#25)，列表带上镜像的 GitHub 编号，方案才对得上本地编号。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from tightrein.assess.issue import blockers
from tightrein.assess.issue.github import mirror_number
from tightrein.implement.prompts.common import (
    NONE,
    acceptance,
    decisions_text,
    designs_text,
    issue_text,
    knowledge_text,
    list_text,
    notes_text,
)
from tightrein.protocol.security import external
from tightrein.store.tables import issues

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext, Risk
    from tightrein.protocol.runtime import Runtime
    from tightrein.store.tables.issues import Issue

DESIGN_ACCEPTED = "用户已同意按设计层面的根因修复：设计问题照常在 flags.design 中标出，但不作为中止理由，照常给出完整方案。"


def variables(runtime: Runtime, context: ImplementContext, *, risk: Risk, replan: Sequence[str],
              feedback: Sequence[str], design_accepted: bool) -> dict[str, str]:
    return {
        "issue": issue_text(context),
        "acceptance": list_text(acceptance(context.body)),
        "notes": notes_text(context),
        "risk": _risk_text(risk),
        "limits": _limits_text(runtime),
        "replan": list_text(replan),
        "decisions": "\n".join(part for part in (DESIGN_ACCEPTED if design_accepted else "",
                                                  decisions_text(context.decisions)) if part),
        "knowledge": knowledge_text(runtime, context),
        "feedback": list_text(feedback),
        "open_issues": open_issues_text(runtime, context),
        "designs": designs_text(runtime),
    }


def open_issues_text(runtime: Runtime, context: ImplementContext) -> str:
    """除本 Issue 外还没合入主干的 Issue：本地编号、GitHub 编号(有镜像时)、状态与标题。标题可能来自外部，整体包住。"""
    lines = [f"- {item.id}{_github(item)}，{item.status}：{item.title}" for item in issues.find(runtime.conn)
             if item.id != context.issue.id and item.status not in blockers.SETTLED]
    return external("open issues", "\n".join(lines)) if lines else NONE


def _github(issue: Issue) -> str:
    number = mirror_number(issue)
    return f"(GitHub #{number})" if number is not None else ""


def _risk_text(risk: Risk) -> str:
    if not risk.high:
        return "常规：没有命中风险规则。"
    return "高风险，逐项说明怎样处理：\n" + list_text(risk.reasons)


def _limits_text(runtime: Runtime) -> str:
    settings = runtime.settings
    cap = settings.get("boundaries.changeCap")
    return "\n".join([
        f"- 单个 PR 改动不超过 {cap['files']} 个文件、{cap['lines']} 行(不含测试、锁文件与生成文件)，是硬上限",
        f"- 禁改文件(不能出现在方案里)：{'、'.join(settings.get('boundaries.protected.forbidden'))}",
        f"- 高风险文件(要改就写进 protectedTouches)：{'、'.join(settings.get('boundaries.protected.highRisk'))}",
    ])
