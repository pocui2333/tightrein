"""组装 improvement-writer 的任务：近期出问题的来源与可改范围；工作目录为本工具仓库的 skills/(只读)。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from tightrein.pipeline.learn.prompts.common import IMPROVEMENT_WRITER, LearnPrompt, section
from tightrein.pipeline.learn.steps.troubles import Trouble
from tightrein.runner.task import RunnerTask, Subject

SCOPE = ("只能修改 skills/ 下各环节的角色说明与参考资料(prompt 类，给出补丁)，或让某个环节改用另一个能力档"
         "(model 类)；不能修改评测用例、评分器、边界检查与本工具的代码。")


def improvement_task(prompt: LearnPrompt, found: Sequence[Trouble], skills: Path) -> RunnerTask:
    text = "\n\n".join(f"### [{item.label}] {item.subject_type} {item.subject_id}：{item.title}\n\n{item.text}"
                       for item in found)
    return prompt.task(IMPROVEMENT_WRITER, IMPROVEMENT_WRITER, Subject("run", prompt.run_id),
                       section("可改范围", SCOPE), section("出问题的来源", text), workdir=skills)
