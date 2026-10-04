"""组装 fix-planner 的执行器任务(redesign/05-fix.md 第 3 步)：角色说明 + 修复规则 + Issue 各节 + 勘察结论(有时) +
风险判定 + 受保护文件清单 + 单个 PR 的改动量上限 + 上一次验证报告或评审意见(含实施中发现的计划缺口) + 用户的决定与补充。
C 通道(大任务)要求先出整体方案并拆成有先后顺序、各自不超过单 PR 上限的子任务。模型按角色配置(stages.fix.roles.fix-planner)，
核心不写死。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tightrein.domain.fix import FixRisk
from tightrein.pipeline.fix.prompts.common import STAGE, FixPrompt, decisions_text, feedback_text, json_block
from tightrein.pipeline.fix.steps.context import FixContext
from tightrein.runner.roles import join, read_only_task
from tightrein.runner.task import RunnerTask

ROLE = "fix-planner"
SCHEMA = "handoff/outputs/fix-plan.schema.json"
LARGE_NOTE = ("## 大任务：整体方案\n\n本 Issue 属于大任务(C 通道)。先给出整体方案，再把它拆成有先后顺序、各自单独成立、"
              "每个都不超过单个 PR 上限的子任务：summary 与 steps 写整体方案中的第一个子任务，其余写进 split；"
              "整体方案交用户确认后，后续子任务生成为子 Issue 依次修复。")


@dataclass(frozen=True)
class PlanInputs:
    scouting: Mapping[str, Any] | None
    risk: FixRisk
    protected: tuple[str, ...]
    max_files: int
    max_lines: int
    review_notes: tuple[str, ...] = ()
    large: bool = False


PLAN_RULES = ("## 计划与验收标准对应要求\n\n"
              "计划中的每一步都必须对应 Issue 验收标准的至少一条(在 acceptanceMapping 中明确指定对应步骤编号)；"
              "无法对应到验收标准的步骤会被程序判为未授权的额外改动并打回。notDoing(不做什么)为必填项，不得省略。"
              "hypothesis 必填：一条因果链、代码证据与精确到行的修改位置；位置会被程序核对。")


def task(prompt: FixPrompt, context: FixContext, inputs: PlanInputs, attempt: int,
         feedback: Sequence[str] = ()) -> RunnerTask:
    limits = (f"## 单个 PR 的改动量上限与受保护文件\n\n改动文件不超过 {inputs.max_files} 个、变更行数不超过 "
              f"{inputs.max_lines} 行(都不含测试文件)。这是硬上限：预估超出时不出大计划，按角色说明拆分为有先后顺序的子任务，"
              "本计划只做第一个，其余写进 split，每个子任务的预估也在上限内。"
              f"受保护文件(路径模式)：{'、'.join(inputs.protected) or '无'}")
    large = LARGE_NOTE if inputs.large else ""
    earlier = []
    if context.verify_report is not None:
        earlier.append(json_block("上一次合并前验证的结果", context.verify_report))
    if inputs.review_notes:
        earlier.append("## 上一轮的评审意见与不通过项\n\n" + "\n".join(f"- {item}" for item in inputs.review_notes))
    earlier.append(decisions_text(context.decisions))
    body = join(prompt.role(ROLE), prompt.rules(), large, PLAN_RULES, context.issue_text(),
                json_block("勘察结论", inputs.scouting) if inputs.scouting is not None else "",
                json_block("风险判定", inputs.risk.to_dict()), limits, *earlier,
                f"## 相关知识\n\n{context.knowledge}", feedback_text(feedback))
    return read_only_task(run_id=prompt.run_id, stage=STAGE, role=ROLE, subject=prompt.subject(context.issue_id),
                          attempt=attempt, prompt=body, workdir=prompt.workdir, output_schema=SCHEMA,
                          role_setting=prompt.role_setting(ROLE, context.complexity))
