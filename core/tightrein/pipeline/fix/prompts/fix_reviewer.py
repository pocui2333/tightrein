"""组装 fix-reviewer 的执行器任务(redesign/05-fix.md 第 8 步，38-external-techniques.md 第 5 项)。

轻量评审：角色说明 + 评审项(修复评分表) + Issue 的结论、根因位置与验收标准 + 已确认的计划 + 相对基准 commit 的 diff +
第 7 步实际结果(复现测试、全部项目检查、diff 统计)与疑似写死的字面量命中；
深度评审盲审：输入只有验收标准、最终 diff、测试与检查的真实输出，不提供写代码模型的分析、说明与会话记录，也不提供计划与上下文。
没有位置(文件与行号)或具体触发条件的意见不作为阻断项。评审专门检查是否只针对测试数据写了特殊处理。
截图评审的任务由 verify 组装(verify/prompts/screenshot_review.py)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tightrein.domain.enums import ReviewMode
from tightrein.domain.issue_sections import ACCEPTANCE, CAUSE, PROBLEM
from tightrein.domain.fix import FixRisk
from tightrein.evaluation import rubric
from tightrein.pipeline.fix.prompts.common import STAGE, FixPrompt, json_block
from tightrein.pipeline.fix.steps.context import FixContext
from tightrein.runner.roles import join, read_only_task, reviewer_setting
from tightrein.runner.task import RunnerTask

ROLE = "fix-reviewer"
SCHEMA = "handoff/outputs/fix-review.schema.json"
REVIEW_SECTIONS = (PROBLEM, CAUSE, ACCEPTANCE)


SPECIAL_CASE = ("## 特判检查\n\n逐处核对新增的条件分支与字面量：是否只针对复现测试或测试数据的输入写了特殊处理"
                "(按输入值、编号或固定字符串分支而不是修正通用逻辑)。发现时记为阻断项，kind 为 hardcode。")


def task(prompt: FixPrompt, context: FixContext, plan: Mapping[str, Any], diff_text: str, checks: Sequence[str],
         mode: ReviewMode, attempt: int, *, risk: FixRisk | None = None, results: str = "") -> RunnerTask:
    if mode is ReviewMode.DEEP:
        # 深度评审盲审：只看验收标准、最终 diff、测试与检查的真实输出
        acceptance = "\n".join(f"- {item}" for item in context.acceptance) or "- 无"
        body = join(prompt.role(ROLE), f"本次为{mode.label}(盲审)，输出的 `mode` 写 `{mode.value}`。",
                    rubric.render(rubric.load(STAGE), "judge"),
                    f"# Issue {context.issue_id} 验收标准\n\n{acceptance}",
                    f"## 最终 diff\n\n```diff\n{diff_text}\n```",
                    f"## 实际结果(第 7 步)\n\n{results}" if results else "",
                    SPECIAL_CASE)
    else:
        issue = "\n\n".join([f"# Issue {context.issue_id}：{context.issue.title}", *context.keyed(REVIEW_SECTIONS)])
        body = join(prompt.role(ROLE), f"本次为{mode.label}，输出的 `mode` 写 `{mode.value}`。",
                    rubric.render(rubric.load(STAGE), "judge"), issue, json_block("已确认的修复计划", plan),
                    f"## 相对基准 commit 的改动\n\n```diff\n{diff_text}\n```",
                    f"## 实际结果(第 7 步)\n\n{results}" if results else "",
                    "## 程序检查的提示\n\n" + ("\n".join(f"- {item}" for item in checks) or "- 无"), SPECIAL_CASE)
    return read_only_task(run_id=prompt.run_id, stage=STAGE, role=f"{ROLE}-{mode.value}",
                          subject=prompt.subject(context.issue_id), attempt=attempt, prompt=body,
                          workdir=prompt.workdir, output_schema=SCHEMA,
                          role_setting=reviewer_setting(prompt.config, f"stages.fix.review.{mode.value}"))
