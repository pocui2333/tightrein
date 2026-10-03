"""score_output：对一次输出执行全部适用的评分项(architecture/03 2.6.4，design 12.3)。

1. 加载该环节的评分表，用例的 excludeItems 与不满足 appliesWhen 的项记为不适用；
2. code 项调用注册的评分器；用例的期望断言作为 code 项(assert-<序号>)一并求值；
3. 全部 judge 项合并为一个评审任务；没有评审执行器或没有代码快照时记为 unknown；
4. score = 通过项 ÷ (适用项 - unknown 项)，分母为 0 时为 0；passed 要求全部适用项都为 pass，有 unknown 即为假。
failed_run 用于被测版本自身失败的运行(交接文档 blocked 或 failed、执行器达到上限或输出不合 schema、没有交接文档)：
全部适用项记为 fail，原因写明失败情形。生产运行与评测调用的是同一份评分表与评分器。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tightrein.domain.clock import Clock
from tightrein.domain.enums import ScoreMethod, ScoreResult, Stage
from tightrein.evaluation import rubric
from tightrein.evaluation.rubric import Rubric, RubricItem
from tightrein.evaluation.scorers import assertions, judge
from tightrein.evaluation.scorers.base import ItemResult, ScoringContext
from tightrein.evaluation.scorers.code import REGISTRY
from tightrein.runner.service import Runner

NO_JUDGE = "没有评审执行器，judge 项交给人判断"
NO_SNAPSHOT = "没有被测项目的代码快照，评审无法核对代码"
NOT_APPLICABLE_CONDITION = "不满足适用条件"


def _excluded(item: RubricItem, context: ScoringContext) -> str | None:
    if context.case is not None and item.id in context.case.exclude_items:
        return f"用例排除：{context.case.exclude_items[item.id]}"
    return None


def _applies(item: RubricItem, outputs: Mapping[str, Any], context: ScoringContext) -> str | None:
    """不适用时返回原因。"""
    excluded = _excluded(item, context)
    if excluded is not None:
        return excluded
    condition = item.applies_when
    if condition is not None and not assertions.holds(condition.op, condition.path, condition.value, outputs)[0]:
        return NOT_APPLICABLE_CONDITION
    return None


def _not_applicable(item: RubricItem, reason: str) -> ItemResult:
    return ItemResult(item.id, item.method, ScoreResult.NOT_APPLICABLE, reason)


def _case_input(context: ScoringContext) -> str | None:
    if context.case is None or not context.case.input_file.is_file():
        return None
    return context.case.input_file.read_text(encoding="utf-8")


def score_output(stage: Stage, handoff: Mapping[str, Any], context: ScoringContext, runner: Runner | None,
                 clock: Clock, loaded: Rubric | None = None) -> list[ItemResult]:
    table = loaded or rubric.load(stage)
    outputs = handoff.get("outputs") or {}
    results: dict[str, ItemResult] = {}
    judged: list[RubricItem] = []
    for item in table.items:
        reason = _applies(item, outputs, context)
        if reason is not None:
            results[item.id] = _not_applicable(item, reason)
        elif item.method is ScoreMethod.CODE and item.scorer is not None:
            outcome = REGISTRY[item.scorer](outputs, item.params, context)
            results[item.id] = ItemResult(item.id, item.method, outcome.result, outcome.reason, outcome.evidence)
        else:
            judged.append(item)
    if judged:
        if runner is None or context.judge is None:
            results.update({result.item_id: result for result in judge.unknown_items(judged, NO_JUDGE)})
        elif context.project_snapshot is None:
            results.update({result.item_id: result for result in judge.unknown_items(judged, NO_SNAPSHOT)})
        else:
            task = judge.judge_task(context.judge, judged, context.project_snapshot, _case_input(context), outputs)
            results.update({result.item_id: result for result in judge.run_judge(runner, clock, task, judged)})
    ordered = [results[item.id] for item in table.items]
    if context.case is not None:
        for assertion in context.case.assertions:
            outcome = assertions.evaluate(assertion, outputs)
            ordered.append(ItemResult(assertion.item_id, ScoreMethod.CODE, outcome.result, outcome.reason,
                                      outcome.evidence))
    return ordered


def failed_run(stage: Stage, handoff: Mapping[str, Any] | None, context: ScoringContext, reason: str,
               loaded: Rubric | None = None) -> list[ItemResult]:
    """没有交接文档时无法判断 appliesWhen，只按用例的排除项确定不适用的项。"""
    table = loaded or rubric.load(stage)
    results = []
    for item in table.items:
        excluded = _excluded(item, context) if handoff is None else _applies(item, handoff.get("outputs") or {},
                                                                             context)
        results.append(_not_applicable(item, excluded) if excluded is not None
                       else ItemResult(item.id, item.method, ScoreResult.FAIL, reason))
    if context.case is not None:
        results += [ItemResult(assertion.item_id, ScoreMethod.CODE, ScoreResult.FAIL, reason)
                    for assertion in context.case.assertions]
    return results


def summarize(items: Sequence[ItemResult]) -> tuple[float, bool]:
    applicable = [item for item in items if item.result is not ScoreResult.NOT_APPLICABLE]
    known = [item for item in applicable if item.result is not ScoreResult.UNKNOWN]
    passes = sum(1 for item in known if item.result is ScoreResult.PASS)
    score = passes / len(known) if known else 0.0
    return score, bool(applicable) and all(item.result is ScoreResult.PASS for item in applicable)

