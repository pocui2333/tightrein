"""前端设计说明(implement.design.frontend)：方案的文件中有前端文件时，在方案通过后、编码之前只读运行一次。

- 前端文件按路径模式判定：项目事实的 frontendPatterns 加缺省模式(controls.implement.design.frontend.paths)；
- 样式取值优先项目既有的设计变量与组件并写明来源；方案没覆盖而设计上必须一起改的写进 planConflicts(定案时
  有冲突就不自动确认)；
- 失败不阻断方案：原因记进方案的交接，编码只按方案实施。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tightrein.implement.prompts import frontend as prompts
from tightrein.implement.prompts.common import Ask, Usage, ask, failure_text
from tightrein.protocol.boundaries import matching_pattern

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext
    from tightrein.protocol.runtime import Runtime

POINT = "implement.design.frontend"
SCHEMA = Path(__file__).with_name("frontend.schema.json")


@dataclass(frozen=True)
class FrontendDesign:
    files: list[str]
    design: dict[str, Any] | None
    error: str | None


def frontend_files(runtime: Runtime, paths: Sequence[str]) -> list[str]:
    """匹配前端路径模式的文件：项目事实中写明的加上缺省模式(写法同受保护文件)。"""
    project = runtime.settings.project
    patterns = (*(project.frontend_patterns if project is not None else ()),
                *runtime.settings.section(POINT).get("paths", ()))
    return [path for path in paths if matching_pattern(path, patterns) is not None]


def design(runtime: Runtime, context: ImplementContext, plan: Mapping[str, Any], usage: Usage) -> FrontendDesign | None:
    """方案没有前端文件时为 None，不调用模型。"""
    files = frontend_files(runtime, [str(item["path"]) for item in plan.get("files") or []])
    if not files:
        return None
    result = ask(runtime, context, Ask(POINT, prompts.variables(runtime, context, plan, files), SCHEMA), usage)
    if result.ok and result.output is not None:
        return FrontendDesign(files, dict(result.output), None)
    return FrontendDesign(files, None, f"前端设计说明没有产出，编码只按方案实施：{failure_text(result)}")
