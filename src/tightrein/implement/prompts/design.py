"""方案(implement.design)的提示变量。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from tightrein.implement.prompts.common import (
    acceptance,
    decisions_text,
    issue_text,
    knowledge_text,
    list_text,
    notes_text,
)

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext, Risk
    from tightrein.protocol.runtime import Runtime

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
    }


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
