"""模型评审(architecture/03 2.6.4，design 12.1、12.5)：同一次输出的全部 judge 项合并为一个执行器任务。

- 任务说明为评审说明(JUDGE_INSTRUCTIONS)、评分表中 judge 项的原文、用例输入与被评的 outputs；评审者看不到生成者的
  会话记录与推理过程；
- 工作目录为被测项目的代码快照，只读，只允许只读的查找命令；输出按 runner/roles/judge.schema.json；
- 输出不合 schema 时由执行器重试一次，仍不合格则全部 judge 项记为 unknown，原因为「评审输出无效」；执行器没有
  完成(无法启动、达到上限)时同样记为 unknown 并写明状态；评审没有给出的项记为 unknown。
评审说明写在这里而不是 skills/ 下：skills/ 属于 improve 可以修改的范围，评分器不属于。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tightrein.domain.clock import Clock
from tightrein.domain.enums import Access, RunnerStatus, ScoreMethod, ScoreResult
from tightrein.evaluation.rubric import RubricItem
from tightrein.evaluation.scorers.base import ItemResult, JudgeRequest
from tightrein.runner.service import Runner
from tightrein.runner.task import Instructions, RunnerTask

ROLE = "judge"
OUTPUT_SCHEMA = "runner/roles/judge.schema.json"
ALLOWED_COMMANDS = ("rg", "ls", "cat", "head", "wc")
INVALID_OUTPUT = "评审输出无效"
NOT_GIVEN = "评审没有给出该项"
JUDGE_INSTRUCTIONS = """你是独立评审。下面给出一次运行的输入与被评的输出(outputs)，以及需要你判断的评分项。

- 逐项独立判断，不因为输出更长、写得更多而加分，也不因为某一项的结论影响另一项。
- 你看不到生成者的推理过程，只根据输出与代码判断；需要核对代码时，在当前目录(被测项目在该次运行所用 commit 的只读快照)中用只读命令查找与阅读。
- 每一项给出 pass、fail 或 unknown：证据足以判定达标给 pass，足以判定不达标给 fail，无法判断时给 unknown 并写明缺什么，交给人判断。
- reason 用一到三句说明理由；evidence 列出支撑判断的「文件路径:行号」或 outputs 内的字段路径。
- 只输出一个符合 schema 的 JSON 对象，items 中每个评分项恰好一条，itemId 与给出的编号一致。
"""


def instructions(items: Sequence[RubricItem], case_input: str | None, outputs: Mapping[str, Any]) -> str:
    parts = [JUDGE_INSTRUCTIONS.strip(), "## 评分项", "\n".join(f"- [{item.id}] {item.text}" for item in items)]
    if case_input is not None:
        parts += ["## 运行的输入", case_input.strip()]
    parts += ["## 被评的 outputs", json.dumps(dict(outputs), ensure_ascii=False, indent=2, sort_keys=True)]
    return "\n\n".join(parts) + "\n"


def judge_task(request: JudgeRequest, items: Sequence[RubricItem], workdir: Path, case_input: str | None,
               outputs: Mapping[str, Any]) -> RunnerTask:
    return RunnerTask(
        run_id=request.run_id, stage=request.stage, role=ROLE, subject=request.subject, attempt=1,
        instructions=Instructions(instructions(items, case_input, outputs)), workdir=workdir,
        output_schema=OUTPUT_SCHEMA, access=Access.READ_ONLY, allowed_commands=ALLOWED_COMMANDS,
        tool=request.tool, model=request.model,
    )


def unknown_items(items: Sequence[RubricItem], reason: str) -> list[ItemResult]:
    return [ItemResult(item.id, ScoreMethod.JUDGE, ScoreResult.UNKNOWN, reason) for item in items]


def run_judge(runner: Runner, clock: Clock, task: RunnerTask, items: Sequence[RubricItem]) -> list[ItemResult]:
    result = runner.run(task, clock=clock)
    if result.status is RunnerStatus.SCHEMA_INVALID:
        return unknown_items(items, INVALID_OUTPUT)
    if result.status is not RunnerStatus.OK or result.output is None:
        return unknown_items(items, f"评审没有完成：{result.status.value} {result.error_type or ''}".strip())
    given = {item["itemId"]: item for item in result.output["items"]}
    judged = []
    for item in items:
        answer = given.get(item.id)
        if answer is None:
            judged.append(ItemResult(item.id, ScoreMethod.JUDGE, ScoreResult.UNKNOWN, NOT_GIVEN))
        else:
            judged.append(ItemResult(item.id, ScoreMethod.JUDGE, ScoreResult(answer["result"]), answer["reason"],
                                     tuple(answer["evidence"])))
    return judged
