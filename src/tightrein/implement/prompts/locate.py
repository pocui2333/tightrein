"""定位(implement.locate)的提示变量与调用条件：Issue 的类型(缺陷或需求)决定找根因还是找落点。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from tightrein.agents.params import FRONTEND
from tightrein.implement.design.frontend import frontend_files
from tightrein.implement.prompts.common import (
    decisions_text,
    designs_text,
    issue_text,
    knowledge_text,
    list_text,
    notes_text,
)

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext
    from tightrein.protocol.runtime import Runtime

BUG = "bug"
# 模板按类型决定找什么：缺陷找根因，需求(用户提的、GitHub 导入的功能请求)找改动的落点与可复用的实现。
# 需求照缺陷的说法去找「根因」时曾跑了 8 分钟、一处笔记也没留下
KINDS = {
    BUG: "`bug`：缺陷，找根因与出问题的位置。",
    "feature": "`feature`：需求，找改动的落点(入口、要修改或扩展的文件与函数)与可复用的现成实现，不找根因。",
}


def variables(runtime: Runtime, context: ImplementContext, feedback: Sequence[str]) -> dict[str, str]:
    return {"kind": KINDS.get(context.issue.kind, KINDS[BUG]), "issue": issue_text(context),
            "notes": notes_text(context), "knowledge": knowledge_text(runtime, context),
            "decisions": decisions_text(context.decisions), "feedback": list_text(feedback),
            "designs": designs_text(runtime)}


def conditions(runtime: Runtime, context: ImplementContext) -> tuple[str, ...]:
    """笔记涉及的文件(根因位置)含前端文件时带 frontend 条件；没有已知位置时不带。"""
    known = context.notes.files if context.notes is not None else []
    return (FRONTEND,) if frontend_files(runtime, known) else ()
