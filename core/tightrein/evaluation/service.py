"""评测的门面(architecture/03 2.4、2.6、2.9，design 14.8)：组织计划、执行、统计与报告，支持中断后续跑。

evaluate：
1. 校验用例(哈希、未提交改动、schema)；被篡改时写一个 gate 事件后停止；
2. 确定用例(计划中为空表示该模块的全部用例)与随机种子，写 plan.json(计划、各用例哈希、manifest 哈希、evals/ 树对象)；
3. 构建各版本快照并做防作弊检查，复制数据库，导出用例涉及的被测项目代码快照；
4. 按「次数 → 用例 → 变体」三层循环运行，同一轮内变体的顺序由种子打乱；每次运行以 --output 沙箱模式启动模块，
   归类结果后评分(失败的运行全部适用项记为 fail)，把 RunScore 追加到 scores.jsonl；
5. 停止条件：累计费用达到 evaluation.budgetUsd 时不再启动新运行；执行器无法启动时立即停止；同一变体连续 3 次
   没有交接文档时停止；这些情况报告为 incomplete，写明原因与续跑命令；
6. 统计、比较、判定，写 report.json 与 report.md。
resume 读取 plan.json，重新校验用例集与 plan.json 记录的哈希一致后只补跑 scores.jsonl 中缺少的运行。
评测本身写一个 run_script span(stage 为 improve)；评测分数只留在评测目录，不写 scores 表。
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from tightrein.config import layers
from tightrein.config.project import ProjectConfig
from tightrein.domain import ids
from tightrein.domain.clock import Clock, format_iso
from tightrein.domain.enums import RunStage, Stage
from tightrein.evaluation import compare, report, rubric, stats, versions
from tightrein.evaluation.cases import ModuleCase, VerifiedCases, verify_cases
from tightrein.evaluation.errors import CaseProblem, EvalCaseInvalid, EvalCaseTampered
from tightrein.evaluation.report import EvaluationReport
from tightrein.evaluation.sandbox import ModuleRunner, SandboxOutcome, SandboxRequest
from tightrein.evaluation.scorers.base import JudgeRequest, ScoringContext
from tightrein.evaluation.scoring import failed_run, score_output, summarize
from tightrein.evaluation.stats import RunScore
from tightrein.evaluation.variants import EvaluationPlan, Variant
from tightrein.guards.policy import GuardSettings
from tightrein.observability.tracing import Tracer
from tightrein.runner.service import Runner
from tightrein.runner.task import Subject
from tightrein.store.files import atomic
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.vcs.process import VcsProcess

SEED_BITS = 32
SUBJECT_TYPES = {Stage.TRIAGE: "problem", Stage.FIX: "issue", Stage.ISSUE: "issue"}
GATE_REJECT = "reject"

RunnerFactory = Callable[[Path], Runner]


@dataclass(frozen=True)
class EvaluationSettings:
    budget_usd: float
    project_repo: Path
    judge_tool: str
    judge_model: str | None = None
    guard_settings: GuardSettings = field(default_factory=GuardSettings)
    max_consecutive_crashes: int = field(
        default_factory=lambda: int(layers.core_value("runtime.evaluation.maxConsecutiveCrashes")))

    @classmethod
    def from_config(cls, config: ProjectConfig) -> EvaluationSettings:
        judge = config.get("evaluation.judge")
        choice = config.capabilities.choose(
            {"tool": judge.get("runner") or config.default_tool, "capability": judge.get("capability")},
            "evaluation.judge")
        return cls(config.get("evaluation.budgetUsd"), config.repo, choice.tool, choice.model,
                   GuardSettings.from_config(config), int(config.get("runtime.evaluation.maxConsecutiveCrashes")))


@dataclass(frozen=True)
class Dependencies:
    layout: WorkspaceLayout
    tool_root: Path
    settings: EvaluationSettings
    module_runner: ModuleRunner
    runner_factory: RunnerFactory | None
    process: VcsProcess
    clock: Clock
    tracer: Tracer


def _gate(deps: Dependencies, error: EvalCaseTampered) -> None:
    deps.tracer.event("gate", decision=GATE_REJECT, reason=str(error).splitlines()[0],
                      attributes={"problems": [str(problem) for problem in error.problems]})


def _verified(deps: Dependencies, module: Stage) -> VerifiedCases:
    try:
        return verify_cases(deps.layout, deps.process, module, rubric.item_ids)
    except EvalCaseTampered as error:
        _gate(deps, error)
        raise


def _read_scores(path: Path) -> list[RunScore]:
    if not path.is_file():
        return []
    return [RunScore.from_dict(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _append_score(path: Path, run: RunScore) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(run.to_dict(), ensure_ascii=False) + "\n")


def evaluate(plan: EvaluationPlan, deps: Dependencies, seed: int | None = None) -> EvaluationReport:
    evaluation_id = ids.eval_id(deps.clock.now())
    verified = _verified(deps, plan.module)
    available = {case.id: case for case in verified.module_cases}
    chosen = plan.case_ids or tuple(sorted(available))
    unknown = [case_id for case_id in chosen if case_id not in available]
    if unknown:
        raise EvalCaseInvalid([CaseProblem(f"{plan.module.value}/{case_id}", "用例不存在") for case_id in unknown])
    plan = replace(plan, case_ids=tuple(chosen))
    record = {
        "evaluationId": evaluation_id, "plan": plan.to_dict(), "manifestSha256": verified.manifest_sha256,
        "evalsTree": verified.evals_tree,
        "caseHashes": {available[case_id].key: verified.case_hashes[available[case_id].key] for case_id in chosen},
        "seed": random.getrandbits(SEED_BITS) if seed is None else seed,
        "createdAt": format_iso(deps.clock.now()),
    }
    atomic.write_text(deps.layout.eval_plan(evaluation_id), json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return _execute(evaluation_id, record, [available[case_id] for case_id in chosen], deps)


def resume(evaluation_id: str, deps: Dependencies) -> EvaluationReport:
    record = json.loads(deps.layout.eval_plan(evaluation_id).read_text(encoding="utf-8"))
    plan = EvaluationPlan.from_dict(record["plan"])
    verified = _verified(deps, plan.module)
    changed = [CaseProblem(key, "与评测开始时的哈希不同") for key, value in record["caseHashes"].items()
               if verified.case_hashes.get(key) != value]
    if verified.manifest_sha256 != record["manifestSha256"]:
        changed.append(CaseProblem("manifest.json", "与评测开始时的哈希不同"))
    if changed:
        error = EvalCaseTampered(changed)
        _gate(deps, error)
        raise error
    available = {case.id: case for case in verified.module_cases}
    return _execute(evaluation_id, record, [available[case_id] for case_id in plan.case_ids], deps)


def _subject(case: ModuleCase) -> Subject:
    return Subject(SUBJECT_TYPES[case.module], case.source["subjectId"])


def _same_as_judge(variant: Variant, settings: EvaluationSettings) -> bool:
    return variant.runner == settings.judge_tool and variant.model in (None, settings.judge_model)


class _Execution:
    """一次评测(或续跑)的执行过程。"""

    def __init__(self, evaluation_id: str, record: Mapping[str, Any], cases: Sequence[ModuleCase],
                 deps: Dependencies) -> None:
        self.evaluation_id = evaluation_id
        self.record = record
        self.plan = EvaluationPlan.from_dict(record["plan"])
        self.cases = cases
        self.deps = deps
        self.layout = deps.layout
        self.runs = _read_scores(self.layout.eval_scores(evaluation_id))
        self.done = {run.key for run in self.runs}
        self.spent = sum(run.usage.cost_usd or 0.0 for run in self.runs)
        self.crashes: dict[str, int] = {}
        self.notes: list[str] = []
        self.run_id = ids.run_id(deps.clock.now(), RunStage.IMPROVE)

    def prepare(self) -> tuple[dict[str, Path], dict[str, Path]]:
        deps = self.deps
        directories = {label: self.layout.eval_version_dir(self.evaluation_id, label) for label in self.plan.versions}
        built = versions.build_versions(self.plan.versions, deps.tool_root, directories, deps.process,
                                        self.layout.database(), self.layout.project)
        snapshots = {case.commit: versions.export_project(
            deps.process, deps.settings.project_repo, case.commit,
            self.layout.eval_project_snapshot(self.evaluation_id, case.commit)) for case in self.cases}
        return built, snapshots

    def order(self, attempt: int, case: ModuleCase) -> list[Variant]:
        variants = list(self.plan.variants)
        random.Random(f"{self.record['seed']}-{attempt}-{case.id}").shuffle(variants)
        return variants

    def stop(self, note: str) -> bool:
        self.notes.append(note)
        return False

    def run_once(self, case: ModuleCase, variant: Variant, attempt: int, snapshot: Path, project: Path) -> bool:
        """执行一次运行；返回假表示评测需要停止。"""
        deps = self.deps
        if self.spent >= deps.settings.budget_usd:
            return self.stop(f"累计费用 {self.spent:.2f} USD 已达到 evaluation.budgetUsd({deps.settings.budget_usd:g})"
                             f"，调整预算后执行 tightrein eval resume {self.evaluation_id}")
        output_dir = self.layout.eval_run_output(self.evaluation_id, variant.label, case.id, attempt)
        outcome = deps.module_runner.run(SandboxRequest(case, variant, snapshot, self.layout.project, output_dir,
                                                        attempt))
        if outcome.unavailable:
            return self.stop(f"变体 {variant.label} 的执行器无法启动(工具未安装或未登录)，这是环境问题；"
                             f"处理后执行 tightrein eval resume {self.evaluation_id}")
        self.spent += outcome.usage.cost_usd or 0.0
        run = self.score(case, variant, attempt, outcome, output_dir, project)
        self.runs.append(run)
        self.done.add(run.key)
        _append_score(self.layout.eval_scores(self.evaluation_id), run)
        crashed = outcome.handoff is None
        self.crashes[variant.label] = self.crashes.get(variant.label, 0) + 1 if crashed else 0
        limit = self.deps.settings.max_consecutive_crashes
        if self.crashes[variant.label] >= limit:
            return self.stop(f"变体 {variant.label} 连续 {limit} 次没有交接文档，"
                             f"检查 {output_dir} 中的 stderr.log 后执行 tightrein eval resume {self.evaluation_id}")
        return True

    def score(self, case: ModuleCase, variant: Variant, attempt: int, outcome: SandboxOutcome, output_dir: Path,
              project: Path) -> RunScore:
        deps = self.deps
        judge = JudgeRequest(self.run_id, Stage.IMPROVE, _subject(case), deps.settings.judge_tool,
                             deps.settings.judge_model)
        context = ScoringContext(case, project, output_dir, outcome.diff_text, deps.settings.guard_settings, judge)
        failure = outcome.failure()
        if failure is not None:
            items = failed_run(case.module, outcome.handoff, context, failure)
        else:
            runner = None if deps.runner_factory is None else deps.runner_factory(output_dir)
            items = score_output(case.module, outcome.handoff or {}, context, runner, deps.clock)
        score, passed = summarize(items)
        return RunScore(case.id, variant.label, attempt, outcome.status, items, score, passed, outcome.usage,
                        outcome.duration_ms, output_dir, failure, outcome.transcripts)

    def execute(self) -> EvaluationReport:
        built, snapshots = self.prepare()
        running = True
        for attempt in range(1, self.plan.repeats + 1):
            for case in self.cases:
                for variant in self.order(attempt, case):
                    if not running or (variant.label, case.id, attempt) in self.done:
                        continue
                    running = self.run_once(case, variant, attempt, built[variant.version.label],
                                            snapshots[case.commit])
        return self.finish()

    def finish(self) -> EvaluationReport:
        labels = [variant.label for variant in self.plan.variants]
        case_ids = [case.id for case in self.cases]
        expected = len(labels) * len(case_ids) * self.plan.repeats
        complete = len(self.done) >= expected and not self.notes
        case_stats = stats.case_stats(self.runs, labels, case_ids)
        comparisons = compare.compare(case_stats, self.plan.baseline.label)
        built = EvaluationReport(
            self.evaluation_id, self.plan, self.record["manifestSha256"], self.record["evalsTree"], case_stats,
            stats.variant_stats(self.runs, case_stats, labels), comparisons,
            compare.verdict(self.plan, comparisons, self.runs, complete), self.runs, list(self.notes),
            [variant.label for variant in self.plan.variants if _same_as_judge(variant, self.deps.settings)],
        )
        return replace(built, report_path=report.write(self.layout, built))


def _execute(evaluation_id: str, record: Mapping[str, Any], cases: Sequence[ModuleCase],
             deps: Dependencies) -> EvaluationReport:
    with deps.tracer.span("run_script", agent="evaluation",
                          attributes={"evaluationId": evaluation_id, "module": record["plan"]["module"]}) as span:
        result = _Execution(evaluation_id, record, cases, deps).execute()
        span.set(decision=None if result.verdict is None else result.verdict.value,
                 artifact=None if result.report_path is None else deps.layout.relative(result.report_path))
    return result
