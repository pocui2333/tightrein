"""组装 fix-executor 写代码的执行器任务(redesign/05-fix.md 第 6 步)：角色说明 + 修复规则 + 任务文档 + 计划 + 复现测试 +
用户的决定与补充；计划带前端设计说明(frontendDesign)时要求按设计实现；修正模式另加不通过项。
续接第 5 步同一会话时(continued)不再重复角色说明与修复规则。在修复 worktree 中可写，只允许项目检查命令、只读 git 命令与
文本搜索；预算按复杂度；计划确认时列出的受保护文件作为批准的受保护路径。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from tightrein.domain.enums import Access
from tightrein.pipeline.fix.prompts.common import (PLAN_FOR_EXECUTOR, STAGE, FixPrompt, decisions_text, json_block,
                                                   plan_view, risk_conditions)
from tightrein.pipeline.fix.steps.context import FixContext
from tightrein.runner.roles import READ_ONLY_COMMANDS, join
from tightrein.runner.task import Instructions, RunnerTask

ROLE = "fix-executor"
SCHEMA = "runner/roles/fix-executor.schema.json"
DESIGN_NOTE = ("## 前端设计说明\n\n计划中的 `frontendDesign` 是前端设计说明。前端部分按它实现：页面与组件结构、布局与信息层级、"
               "交互与加载、空、出错状态、样式取值(优先项目既有的设计变量与组件)、手机端适配与文案；"
               "需要偏离时写进 `deviations` 并说明理由。")


CODE_ROUND = ("## 这一轮写代码\n\n按任务与计划修改代码，让复现测试通过、其余检查不受影响。不得改动或删除复现测试与其他已有测试，"
              "不加跳过标记，不针对测试数据写特殊处理。")


def task(prompt: FixPrompt, context: FixContext, plan: Mapping[str, Any], attempt: int, *,
         check_commands: Sequence[str], approved_protected: Sequence[str], budget: float | None,
         task_text: str = "", repro_test: str = "", corrections: Sequence[str] = (),
         continued: bool = False) -> RunnerTask:
    acceptance = "\n".join(f"- {item}" for item in context.acceptance) or "- 无"
    fixes = ""
    if corrections:
        fixes = join("## 按检查与评审意见修改", "只改下列不通过项指出的位置，修不了的如实说明：",
                     "\n".join(f"- {item}" for item in corrections))
    head = () if continued else (prompt.role(ROLE), prompt.rules(), task_text)
    body = join(*head, CODE_ROUND, json_block("已确认的修复计划", plan_view(plan, PLAN_FOR_EXECUTOR), "plan"),
                DESIGN_NOTE if plan.get("frontendDesign") else "",
                f"## 复现测试\n\n{repro_test}" if repro_test else "",
                f"## Issue {context.issue_id} 的验收标准\n\n{acceptance}", decisions_text(context.decisions), fixes)
    setting = prompt.role_setting(ROLE, context.complexity, risk_conditions(plan))
    limits = setting.limits if budget is None else replace(setting.limits, max_cost_usd=budget)
    return RunnerTask(
        run_id=prompt.run_id, stage=STAGE, role=ROLE, subject=prompt.subject(context.issue_id), attempt=attempt,
        instructions=Instructions(body), workdir=prompt.workdir, output_schema=SCHEMA, access=Access.WORKSPACE_WRITE,
        allowed_commands=(*dict.fromkeys(check_commands), *READ_ONLY_COMMANDS), limits=limits,
        route=setting.route, conditions=setting.conditions,
        approved_protected_paths=tuple(approved_protected),
    )
