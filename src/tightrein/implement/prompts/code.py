"""编码(implement.code)的提示变量、允许的命令与续接。

- 编码只拿方案中实施要用的字段(PLAN_FOR_CODE)；根因假说只留因果链与修改位置，不带证据；
- 方案与代码笔记都已放进提示，不再列出方案与定位交接文件的路径，免得模型再去读一遍；
- 下一轮续接上一轮的同一会话(implement.code.continue)，不再重复角色、方案与规则，用上提示缓存；条件不同
  (风险判定变了)时工具与模型可能不同，不续接。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from tightrein.implement.check.affected import ProjectCommand, argv_text
from tightrein.implement.prompts.common import (
    NONE,
    PLAN_FOR_CODE,
    decisions_text,
    issue_text,
    json_text,
    knowledge_text,
    list_text,
    notes_text,
    plan_view,
)

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext
    from tightrein.protocol.runtime import Runtime

CONTINUE = "implement.code.continue"
CHECK_COMMANDS = ("test", "lint", "typecheck", "build")
FRONTEND_NOTE = "前端部分按下面的设计说明实现；偏离写进 deviations 并说明理由。\n\n"


def variables(runtime: Runtime, context: ImplementContext, design: Mapping[str, Any],
              corrections: Sequence[str]) -> dict[str, str]:
    frontend = design.get("frontendDesign")
    return {
        "issue": issue_text(context),
        "acceptance": list_text(design.get("acceptance") or []),
        "plan": json_text(plan_view(design, PLAN_FOR_CODE)),
        "frontend": FRONTEND_NOTE + json_text(frontend) if frontend else NONE,
        "notes": notes_text(context),
        "commands": list_text([f"`{command}`" for command in allowed_commands(runtime)]),
        "corrections": list_text(corrections),
        "decisions": decisions_text(context.decisions),
        "knowledge": knowledge_text(runtime, context),
    }


def continue_variables(context: ImplementContext, corrections: Sequence[str]) -> dict[str, str]:
    return {"round": str(context.round), "corrections": list_text(corrections),
            "decisions": decisions_text(context.decisions)}


def allowed_commands(runtime: Runtime) -> tuple[str, ...]:
    """项目的检查命令；测试命令带 `{tests}` 的，去掉占位后的前缀也算(只跑受影响的测试要用)。只读命令由
    agents.params_for 统一加上。"""
    project = runtime.settings.project
    configured = project.commands if project is not None else {}
    return tuple(dict.fromkeys(argv_text(ProjectCommand(name, str(configured[name])), ())
                               for name in CHECK_COMMANDS if configured.get(name)))
