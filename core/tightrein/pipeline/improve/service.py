"""自我改进只建议(redesign/08-learn.md 第 1 节)：从近期的失败中归纳一条对提示词或模型档的修改建议，附评测对比，
写成 decision 文档进收件箱，由用户批准后自己应用；程序不修改任何提示、配置或代码。

suggest：
1. 出问题的来源(learn 的 troubles，learn.improve.lookbackDays 天内)少于 learn.improve.minTroubles 时不调用模型；
2. improvement-writer 归纳一条建议(没有共同原因时为空)；
3. 校验：prompt 类补丁只改 skills/ 下的文件，且不触及评测、边界与 improve 本身(evaluation.versions.FORBIDDEN_PATTERNS)；
4. 评测：该环节的用例分为参与改进(来源对象在 addresses 或出问题的来源中)与未参与改进；后者少于
   learn.improve.minHeldOutCases 时不出建议。prompt 类以 HEAD 加补丁为候选，model 类以建议的能力档对应的模型对比当前模型；
5. 汇总两组的确定性项(code 评分项)通过率与平均分；评测完整、未参与改进的用例确定性通过率没有下降、参与改进的有提升时
   推荐批准，否则推荐拒绝；
6. 写 data/improve/<建议编号>.md(decision)与 .patch，建议记录 kind improvement。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta, tzinfo
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock
from tightrein.domain.enums import (
    EvalVerdict,
    HandoffStatus,
    RunnerStatus,
    RunStage,
    ScoreMethod,
    ScoreResult,
    Stage,
    SuggestionKind,
)
from tightrein.domain.handoff.document import Reference
from tightrein.evaluation.cases import ModuleCase
from tightrein.evaluation.report import EvaluationReport
from tightrein.evaluation.variants import CANDIDATE, HEAD, EvaluationPlan, VersionSpec, tool_model_plan, version_plan
from tightrein.evaluation.versions import FORBIDDEN_PATTERNS
from tightrein.observability.events import EventLog
from tightrein.pipeline.common import stage_runs
from tightrein.pipeline.common.stage_runs import StageRun
from tightrein.pipeline.improve.prompts import improvement_task
from tightrein.pipeline.learn.prompts.common import LearnCalls, LearnPrompt
from tightrein.pipeline.learn.render import decision
from tightrein.pipeline.learn.render.decision import SuggestionText
from tightrein.pipeline.learn.steps import suggestions, troubles
from tightrein.pipeline.learn.steps.suggestions import Draft
from tightrein.runner.roles import Overrides
from tightrein.runner.service import Runner
from tightrein.store.files import atomic
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout

STAGE = RunStage.LEARN
SUBJECT = "run"
PATCH_SUFFIX = ".patch"
SKILLS_PREFIX = "skills/"
PROMPT = "prompt"
INVOLVED, HELD_OUT = "involved", "heldOut"
DIFF_PATH = "+++ b/"

CaseLoader = Callable[[Stage], Sequence[ModuleCase]]
Evaluate = Callable[[EvaluationPlan], EvaluationReport]


@dataclass
class ImproveDeps:
    layout: WorkspaceLayout
    tool: ToolLayout
    config: ProjectConfig
    conn: sqlite3.Connection
    clock: Clock
    events: EventLog
    runner: Runner
    cases: CaseLoader
    evaluate: Evaluate
    zone: tzinfo | None = None
    overrides: Overrides = field(default_factory=Overrides)


@dataclass(frozen=True)
class ImproveResult:
    run_id: str
    handoff: Path
    outputs: Mapping[str, Any]
    suggestion_id: str | None = None


def patch_paths(patch: str) -> list[str]:
    return [line[len(DIFF_PATH):].strip() for line in patch.splitlines() if line.startswith(DIFF_PATH)]


def patch_problem(patch: str | None) -> str | None:
    """prompt 类补丁的可改范围检查；合格时为 None。"""
    paths = patch_paths(patch or "")
    if not paths:
        return "补丁为空或不是统一格式"
    outside = [path for path in paths if not path.startswith(SKILLS_PREFIX)]
    forbidden = [path for path in paths if any(fnmatch(path, pattern) for pattern in FORBIDDEN_PATTERNS)]
    if outside or forbidden:
        return f"补丁只能改 skills/ 下的角色说明与参考资料：{'、'.join([*outside, *forbidden])}"
    return None


def split_cases(cases: Sequence[ModuleCase], subjects: set[str]) -> tuple[list[str], list[str]]:
    """(参与改进的用例, 未参与改进的用例)：来源对象在 subjects 中的为参与改进。"""
    involved = [case.id for case in cases if str(case.source.get("subjectId")) in subjects]
    return involved, [case.id for case in cases if case.id not in involved]


def _group(report: EvaluationReport, variant: str, case_ids: Sequence[str]) -> dict[str, float | None]:
    runs = [run for run in report.runs if run.variant == variant and run.case_id in case_ids]
    items = [item for run in runs for item in run.items
             if item.method is ScoreMethod.CODE and item.result is not ScoreResult.NOT_APPLICABLE]
    passed = sum(item.result is ScoreResult.PASS for item in items)
    return {"deterministic": passed / len(items) if items else None,
            "mean": sum(run.score for run in runs) / len(runs) if runs else None}


def summarize(report: EvaluationReport, involved: Sequence[str], held_out: Sequence[str]) -> dict[str, Any]:
    baseline, candidate = (variant.label for variant in report.plan.variants[:2])
    return {name: {"baseline": _group(report, baseline, cases), "candidate": _group(report, candidate, cases)}
            for name, cases in ((INVOLVED, involved), (HELD_OUT, held_out))}


def recommend(report: EvaluationReport, summary: Mapping[str, Any]) -> tuple[bool, str]:
    if report.verdict is EvalVerdict.INCOMPLETE:
        return False, "评测没有完成：" + "；".join(report.notes)
    if report.verdict is EvalVerdict.REJECT:
        return False, "有用例的得分明显变差"

    def rate(group: str, variant: str) -> float:
        value = summary[group][variant]["deterministic"]
        return -1.0 if value is None else value

    if rate(HELD_OUT, "candidate") < rate(HELD_OUT, "baseline"):
        return False, "未参与改进的用例确定性检查通过率下降，改动可能只对这几次失败有效"
    if rate(INVOLVED, "candidate") <= rate(INVOLVED, "baseline"):
        return False, "参与改进的用例确定性检查通过率没有提升"
    return True, "参与改进的用例确定性检查通过率提升，未参与改进的用例没有变差"


def _percent(value: float | None) -> str:
    return "无" if value is None else f"{value:.0%}"


class ImproveService:
    def __init__(self, deps: ImproveDeps) -> None:
        self.deps = deps

    def _finish(self, run: StageRun, outputs: Mapping[str, Any], next_action: str) -> Path:
        path = run.handoff(STAGE, run.id, HandoffStatus.OK, {"improve": dict(outputs)}, next_action,
                           subject_type=SUBJECT)
        run.end(HandoffStatus.OK)
        return path

    def _done(self, run: StageRun, outputs: dict[str, Any]) -> ImproveResult:
        return ImproveResult(run.id, self._finish(run, outputs, "无需处理"), outputs)

    def suggest(self, days: int | None = None) -> ImproveResult:
        deps = self.deps
        run = stage_runs.begin(STAGE, deps.layout, deps.conn, deps.clock, deps.events)
        lookback = days or deps.config.whole_threshold("learn.improve.lookbackDays")
        found = troubles.collect(deps.conn, deps.layout, deps.clock.now() - timedelta(days=lookback))
        outputs: dict[str, Any] = {"troubles": len(found), "suggestionId": None, "evaluationId": None, "reason": None}
        if len(found) < deps.config.whole_threshold("learn.improve.minTroubles"):
            outputs["reason"] = f"{lookback} 天内出问题的来源只有 {len(found)} 条，不足以归纳"
            return self._done(run, outputs)
        prompt = LearnPrompt(deps.tool, deps.config, run.id, deps.layout.knowledge_dir())
        calls = LearnCalls(deps.runner, deps.clock, prompt, deps.overrides, deps.conn)
        result = calls.run(improvement_task(prompt, found, deps.tool.skills_dir()))
        if result.status is not RunnerStatus.OK or result.output is None:
            outputs["reason"] = f"improvement-writer 没有给出结果：{result.status.value} {result.error_type or ''}".strip()
            return self._done(run, outputs)
        proposed = result.output["suggestion"]
        if proposed is None:
            outputs["reason"] = f"没有可归纳的规律：{result.output['reason']}"
            return self._done(run, outputs)
        return self._evaluate(run, outputs, found, dict(proposed))

    def _plan(self, proposed: Mapping[str, Any], case_ids: Sequence[str], patch: Path) -> EvaluationPlan | str:
        deps = self.deps
        stage = Stage(proposed["stage"])
        repeats = deps.config.whole_threshold("learn.improve.repeats")
        current = deps.config.model_choice(stage)
        if proposed["target"] == PROMPT:
            return version_plan(stage, VersionSpec(CANDIDATE, HEAD, patch=patch), current.tool, current.model,
                                case_ids, repeats)
        if not proposed["capability"]:
            return "model 类建议没有给出能力档"
        candidate = deps.config.model_choice(stage, capability=proposed["capability"])
        if candidate.tool != current.tool or candidate.model == current.model:
            return f"能力档 {proposed['capability']} 与当前使用的模型相同，没有可对比的候选"
        return tool_model_plan(stage, [current.tool], [current.model, candidate.model], case_ids=case_ids,
                               repeats=repeats)

    def _evaluate(self, run: StageRun, outputs: dict[str, Any], found: Sequence[troubles.Trouble],
                  proposed: dict[str, Any]) -> ImproveResult:
        deps = self.deps
        stage = Stage(proposed["stage"])
        problem = patch_problem(proposed["patch"]) if proposed["target"] == PROMPT else None
        if problem is not None:
            outputs["reason"] = problem
            return self._done(run, outputs)
        subjects = {item.subject_id for item in found} | set(proposed["addresses"])
        involved, held_out = split_cases(deps.cases(stage), subjects)
        minimum = deps.config.whole_threshold("learn.improve.minHeldOutCases")
        if len(held_out) < minimum:
            outputs["reason"] = (f"{stage.value} 的评测用例中未参与改进的只有 {len(held_out)} 个，少于 {minimum} 个，"
                                 "无法确认改动对其他任务没有副作用；先用 eval add 补充用例")
            return self._done(run, outputs)
        patch_file = deps.layout.improve_dir() / f"{run.id}{PATCH_SUFFIX}"
        if proposed["target"] == PROMPT and proposed["patch"]:
            atomic.write_text(patch_file, proposed["patch"].rstrip("\n") + "\n")
        try:
            plan = self._plan(proposed, [*involved, *held_out], patch_file)
            if isinstance(plan, str):
                outputs["reason"] = plan
                return self._done(run, outputs)
            report = deps.evaluate(plan)
            summary = summarize(report, involved, held_out)
            recommended, reason = recommend(report, summary)
            outputs.update(evaluationId=report.evaluation_id, summary=summary, recommended=recommended)
            draft = self._draft(run, proposed, found, report, summary, recommended, reason, involved, held_out)
            created = suggestions.store(deps.conn, deps.clock, [draft], self._writer(patch_file))
        finally:
            # 临时补丁只在建议建成时由 _writer 移到建议编号下；去重丢弃、没有评测计划或出错时删掉
            patch_file.unlink(missing_ok=True)
        outputs["suggestionId"] = created[0].id if created else None
        outputs["reason"] = reason if created else "同一建议已在等待处理"
        path = self._finish(run, outputs, f"在收件箱中处理建议 {outputs['suggestionId']}" if created else "无需处理")
        return ImproveResult(run.id, path, outputs, outputs["suggestionId"])

    def _writer(self, patch_file: Path) -> suggestions.DocumentWriter:
        deps = self.deps

        def write(suggestion_id: str, draft: Draft) -> Path:
            if patch_file.is_file():
                final = deps.layout.improve_file(suggestion_id, PATCH_SUFFIX)
                atomic.write_text(final, patch_file.read_text(encoding="utf-8"))
                patch_file.unlink()
            text = draft.document or SuggestionText(draft.subject, "", "", "", False, "", "")
            path = decision.write(deps.layout, suggestion_id, draft.subject, text, deps.clock.now(),
                                  deps.config.language, deps.zone)
            return Path(deps.layout.relative(path))

        return write

    def _draft(self, run: StageRun, proposed: Mapping[str, Any], found: Sequence[troubles.Trouble],
               report: EvaluationReport, summary: Mapping[str, Any], recommended: bool, reason: str,
               involved: Sequence[str], held_out: Sequence[str]) -> Draft:
        deps = self.deps
        stage = proposed["stage"]
        lines = [f"- [{item.label}] {item.subject_id} {item.title}" for item in found
                 if item.subject_id in proposed["addresses"]] or [f"- 共 {len(found)} 条出问题的来源"]
        table = ["| 用例组 | 基线确定性通过率 | 候选确定性通过率 | 基线平均分 | 候选平均分 |", "|---|---|---|---|---|"]
        for name, label in ((INVOLVED, f"参与改进({len(involved)} 个)"), (HELD_OUT, f"未参与改进({len(held_out)} 个)")):
            group = summary[name]
            table.append(f"| {label} | {_percent(group['baseline']['deterministic'])} | "
                         f"{_percent(group['candidate']['deterministic'])} | {_percent(group['baseline']['mean'])} | "
                         f"{_percent(group['candidate']['mean'])} |")
        if proposed["target"] == PROMPT:
            change = f"修改 {stage} 的角色说明：{'、'.join(patch_paths(proposed['patch'] or ''))}"
            apply = "在本工具仓库用 `git apply` 应用工作区 data/improve/ 下同名的 .patch 补丁，提交后生效"
        else:
            change = f"{stage} 环节改用能力档 {proposed['capability']}"
            apply = f"在工作区 project.yaml 写 `stages.{stage}.capability: {proposed['capability']}`"
        text = SuggestionText(
            conclusion=f"{change}；评测{'支持' if recommended else '不支持'}采纳",
            background="\n\n".join([f"依据：{proposed['rationale']}", "针对的失败：\n" + "\n".join(lines),
                                    f"预期：{proposed['expected']}",
                                    f"评测 {report.evaluation_id}(以确定性检查为主)：\n\n" + "\n".join(table)]),
            accept=change, reject="不采纳，保持现状", recommended=recommended, reason=reason, apply=apply,
            references=() if report.report_path is None else
            (Reference(deps.layout.relative(report.report_path), "评测报告"),))
        target = ",".join(patch_paths(proposed["patch"] or "")) or proposed["capability"]
        subject = f"{proposed['target']}:{stage}:{target}"
        return Draft(SuggestionKind.IMPROVEMENT, subject,
                     {"runId": run.id, "stage": stage, "target": proposed["target"],
                      "evaluationId": report.evaluation_id, "summary": dict(summary), "recommended": recommended,
                      "advice": {"recommendation": "批准" if recommended else "拒绝", "reason": reason}},
                     tuple(sorted(proposed["addresses"])), text, proposed["patch"])
