"""组装 lesson-writer 的两种任务(architecture/08 6.1、6.4)：出问题的来源写经验、同类条目的矛盾比对。"""

from __future__ import annotations

from collections.abc import Sequence

from tightrein.pipeline.learn.prompts.common import LESSON_WRITER, LearnPrompt, section
from tightrein.pipeline.learn.steps.troubles import Trouble
from tightrein.retrieval.models import EntryDocument
from tightrein.runner.task import RunnerTask, Subject

LESSON_MODE = "模式：lesson。把下面的来源写成一条经验。"
COMPARE_MODE = "模式：compare。比对下面的同类条目，找出相互矛盾与相互重复的组。"
COMPARE_ROLE = "lesson-writer-compare"


def lesson_task(prompt: LearnPrompt, trouble: Trouble, material: str, existing: str) -> RunnerTask:
    """material 为分诊误判的发现报告或其余来源的修复报告。"""
    title = "发现报告" if trouble.subject_type == "problem" else "修复报告"
    return prompt.task(LESSON_WRITER, LESSON_WRITER, Subject(trouble.subject_type, trouble.subject_id), LESSON_MODE,
                       section(f"来源：{trouble.label}", trouble.text), section(title, material),
                       section("同类已有条目", existing))


def compare_task(prompt: LearnPrompt, number: int, entries: Sequence[EntryDocument]) -> RunnerTask:
    text = "\n\n".join(f"### {entry.id}({entry.path})\n\n{entry.body.strip()}" for entry in entries)
    return prompt.task(LESSON_WRITER, f"{COMPARE_ROLE}-{number}", Subject("run", prompt.run_id), COMPARE_MODE,
                       section("条目", text))
