"""前端设计说明(implement.design.frontend)的提示变量。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from tightrein.implement.prompts.common import decisions_text, issue_text, json_text, knowledge_text, plan_view

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext
    from tightrein.protocol.runtime import Runtime

PLAN_FOR_FRONTEND = ("summary", "steps", "files", "notDoing", "userVisibleChange", "affectedPages")


def variables(runtime: Runtime, context: ImplementContext, plan: Mapping[str, Any],
              files: Sequence[str]) -> dict[str, str]:
    return {"issue": issue_text(context), "plan": json_text(plan_view(plan, PLAN_FOR_FRONTEND)),
            "files": "\n".join(f"- `{path}`" for path in files), "decisions": decisions_text(context.decisions),
            "knowledge": knowledge_text(runtime, context)}
