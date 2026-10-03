"""组装查重任务(architecture/06 4.4)：任务说明 + 本问题的主张 + 候选列表(编号、标题、根因位置、状态)。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tightrein.runner.roles import TASKS, join, read_only_task
from tightrein.pipeline.triage.prompts.common import STAGE, PromptContext, feedback_text
from tightrein.pipeline.triage.steps.claims import Claim
from tightrein.runner.task import RunnerTask, Subject

ROLE = "triage-dedup"
TASK = "dedup"
SCHEMA = "runner/tasks/triage-dedup.schema.json"


@dataclass(frozen=True)
class Candidate:
    id: str
    kind: str
    title: str
    root_causes: tuple[str, ...]
    status: str

    def render(self) -> str:
        causes = "、".join(self.root_causes) or "(没有根因位置)"
        return f"- {self.id}({self.kind}，{self.status})：{self.title}；根因 {causes}"


def task(ctx: PromptContext, problem_id: str, claim: Claim, candidates: Sequence[Candidate], attempt: int,
         feedback: Sequence[str] = ()) -> RunnerTask:
    listed = "\n".join(candidate.render() for candidate in candidates)
    prompt = join(ctx.text("references/tasks/dedup.md"), claim.render(), f"## 候选\n\n{listed}",
                  feedback_text(feedback))
    return read_only_task(run_id=ctx.run_id, stage=STAGE, role=ROLE, subject=Subject("problem", problem_id),
                          attempt=attempt, prompt=prompt, workdir=ctx.workdir, output_schema=SCHEMA,
                          role_setting=ctx.role_setting(TASKS, TASK))
