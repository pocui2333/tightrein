"""组装 fix-scout 的执行器任务(redesign/05-fix.md 第 2 步)：角色说明 + 修复规则 + Issue 各节 + 根因位置 + 预取条目 +
用户的决定与补充。只读地找根因位置、联动方、可复用的现成实现与复现线索，不做任何修改。模型按调用点 fix.scout 的路由；
Issue 的根因位置中有前端文件(frontend_designer.PATHS 的路径模式)时带 frontend 条件，没有已知位置时不带。"""

from __future__ import annotations

from collections.abc import Sequence

from tightrein.config.project import ProjectConfig
from tightrein.config.routes import FRONTEND
from tightrein.guards.protected import matching_pattern
from tightrein.pipeline.fix.prompts.common import STAGE, FixPrompt, decisions_text, feedback_text
from tightrein.pipeline.fix.prompts.frontend_designer import PATHS
from tightrein.pipeline.fix.steps.context import FixContext
from tightrein.runner.roles import join, read_only_task
from tightrein.runner.task import RunnerTask

ROLE = "fix-scout"
SCHEMA = "runner/roles/fix-scout.schema.json"


def conditions(config: ProjectConfig, context: FixContext) -> tuple[str, ...]:
    patterns = tuple(config.get(PATHS))
    frontend = any(matching_pattern(path, patterns) is not None for path in context.root_files)
    return (FRONTEND,) if frontend else ()


def task(prompt: FixPrompt, context: FixContext, attempt: int, feedback: Sequence[str] = ()) -> RunnerTask:
    roots = "\n".join(f"- {location}" for location in context.issue.root_cause) or "- 未定位"
    body = join(prompt.role(ROLE), prompt.rules(), context.issue_text(), f"## 根因位置\n\n{roots}",
                f"## 相关知识\n\n{context.knowledge}", decisions_text(context.decisions), feedback_text(feedback))
    return read_only_task(run_id=prompt.run_id, stage=STAGE, role=ROLE, subject=prompt.subject(context.issue_id),
                          attempt=attempt, prompt=body, workdir=prompt.workdir, output_schema=SCHEMA,
                          role_setting=prompt.role_setting(ROLE, context.complexity,
                                                           conditions(prompt.config, context)))
