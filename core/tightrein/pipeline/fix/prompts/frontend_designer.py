"""组装 frontend-designer 的执行器任务(architecture/07 4.6)：修复计划预估改动的文件中有前端文件时，在出计划之后、
实施之前运行一次，只读；角色说明 + Issue 各节 + 已通过检查的修复计划 + 其中的前端文件 + 预取条目 + 用户的决定与补充。
前端文件按 stages.fix.roles.frontend-designer.paths 的路径模式判定(写法同 protectedPaths，可用 paths+ 追加)。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.guards.protected import matching_pattern
from tightrein.pipeline.fix.prompts.common import STAGE, FixPrompt, decisions_text, json_block
from tightrein.pipeline.fix.steps.context import FixContext
from tightrein.runner.roles import join, read_only_task
from tightrein.runner.task import RunnerTask

ROLE = "frontend-designer"
SCHEMA = "runner/roles/frontend-designer.schema.json"
PATHS = "stages.fix.roles.frontend-designer.paths"


def frontend_files(config: ProjectConfig, plan: Mapping[str, Any]) -> list[str]:
    """计划预估改动的文件中匹配前端路径模式的文件。"""
    patterns = tuple(config.get(PATHS))
    return [item["path"] for item in plan["files"] if matching_pattern(item["path"], patterns) is not None]


def task(prompt: FixPrompt, context: FixContext, plan: Mapping[str, Any], files: Sequence[str],
         attempt: int) -> RunnerTask:
    body = join(prompt.role(ROLE), context.issue_text(), json_block("修复计划", plan),
                "## 计划中的前端文件\n\n" + "\n".join(f"- `{path}`" for path in files),
                f"## 相关知识\n\n{context.knowledge}", decisions_text(context.decisions))
    return read_only_task(run_id=prompt.run_id, stage=STAGE, role=ROLE, subject=prompt.subject(context.issue_id),
                          attempt=attempt, prompt=body, workdir=prompt.workdir, output_schema=SCHEMA,
                          role_setting=prompt.role_setting(ROLE, context.complexity))
