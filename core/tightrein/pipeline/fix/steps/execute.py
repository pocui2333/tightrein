"""写代码(redesign/05-fix.md 第 6 步)：调用 fix-executor，按任务与计划实施或按不通过项修正；session_id 给出时续接
第 5 步(或上一轮)的同一会话。

guards 由执行器在任务前后检查(改动只在 worktree 内、没有新提交与新分支、没有改动未确认的受保护文件、没有改动测试与
复现检查、没有读取隐藏路径与凭证)；违规时执行器返回 guard-violation，由调用方按「规范要求用户确认」处理。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tightrein.domain.enums import RunnerStatus
from tightrein.guards.report import Violation
from tightrein.pipeline.fix.prompts import fix_executor
from tightrein.pipeline.fix.prompts.common import FixCalls
from tightrein.pipeline.fix.steps.context import FixContext
from tightrein.pipeline.checks.project_checks import TESTS_PLACEHOLDER, CheckCommand

ROLE = fix_executor.ROLE
ABORTED = "aborted"


@dataclass(frozen=True)
class Execution:
    output: Mapping[str, Any] | None
    status: RunnerStatus
    violations: tuple[Violation, ...] = ()
    error: str | None = None
    session_id: str | None = None

    @property
    def aborted(self) -> bool:
        return self.output is not None and self.output["status"] == ABORTED


def allowed_commands(commands: Sequence[CheckCommand]) -> list[str]:
    found = [command.command for command in commands]
    found += [command.affected.command.replace(TESTS_PLACEHOLDER, "").strip() for command in commands
              if command.affected is not None]
    return list(dict.fromkeys(found))


def execute(calls: FixCalls, context: FixContext, plan: Mapping[str, Any], attempt: int, *,
            commands: Sequence[CheckCommand], approved_protected: Sequence[str], budget: float | None,
            task_text: str = "", repro_test: str = "", corrections: Sequence[str] = (),
            session_id: str | None = None) -> Execution:
    task = fix_executor.task(calls.prompt, context, plan, attempt, check_commands=allowed_commands(commands),
                             approved_protected=approved_protected, budget=budget, task_text=task_text,
                             repro_test=repro_test, corrections=corrections, continued=session_id is not None)
    result = calls.run(task, resume_session=session_id)
    output = result.output if result.status is RunnerStatus.OK else None
    return Execution(output, result.status, result.violations, result.error_type, result.session_id or session_id)
