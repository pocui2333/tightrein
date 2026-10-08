"""定位(implement.locate)的提示变量与调用条件。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from tightrein.agents.params import FRONTEND
from tightrein.implement.design.frontend import frontend_files
from tightrein.implement.prompts.common import (
    decisions_text,
    issue_text,
    knowledge_text,
    list_text,
    notes_text,
)

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext
    from tightrein.protocol.runtime import Runtime


def variables(runtime: Runtime, context: ImplementContext, feedback: Sequence[str]) -> dict[str, str]:
    return {"issue": issue_text(context), "notes": notes_text(context), "knowledge": knowledge_text(runtime, context),
            "decisions": decisions_text(context.decisions), "feedback": list_text(feedback)}


def conditions(runtime: Runtime, context: ImplementContext) -> tuple[str, ...]:
    """笔记涉及的文件(根因位置)含前端文件时带 frontend 条件；没有已知位置时不带。"""
    known = context.notes.files if context.notes is not None else []
    return (FRONTEND,) if frontend_files(runtime, known) else ()
