"""组装 claim-verifier 与 refuter 的执行器任务(architecture/06 4.5、4.7)。

refuter 拿到与第一次取证完全相同的主张、事实与相关知识，看不到第一次的判定与输出；它的工具与模型取
stages.triage.refuter(配置时)，输出 schema 与 claim-verifier 相同。两个角色在 fp-check 已安装时加载它。
"""

from __future__ import annotations

from collections.abc import Sequence

from tightrein.domain.enums import Complexity
from tightrein.runner.roles import FP_CHECK, ROLES, installed_skills, join, read_only_task
from tightrein.pipeline.triage.prompts.common import (
    EVIDENCE_STANDARD,
    STAGE,
    PromptContext,
    feedback_text,
    knowledge_text,
    rubric_text,
    severity_text,
)
from tightrein.pipeline.triage.steps.claims import Claim
from tightrein.runner.task import RunnerTask, Subject

CLAIM_VERIFIER = "claim-verifier"
REFUTER = "refuter"
SCHEMAS = {CLAIM_VERIFIER: "runner/roles/claim-verifier.schema.json", REFUTER: "runner/roles/refuter.schema.json"}


def task(ctx: PromptContext, role: str, problem_id: str, claim: Claim, knowledge: str, complexity: Complexity,
         attempt: int, feedback: Sequence[str] = ()) -> RunnerTask:
    prompt = join(ctx.text(f"references/roles/{role}.md"), ctx.text(EVIDENCE_STANDARD),
                  severity_text(ctx.tool, ctx.config), rubric_text(),
                  claim.render(), knowledge_text(knowledge), feedback_text(feedback))
    return read_only_task(run_id=ctx.run_id, stage=STAGE, role=role, subject=Subject("problem", problem_id),
                          attempt=attempt, prompt=prompt, workdir=ctx.workdir, output_schema=SCHEMAS[role],
                          role_setting=ctx.role_setting(ROLES, role, complexity),
                          skills=installed_skills(ctx.tool, (FP_CHECK,)))
