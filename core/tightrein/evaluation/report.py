"""评测报告(architecture/03 2.8，design 10.3)：机读的 report.json 与人读的 report.md。

report.json 符合 data/eval-report.schema.json 的 module 分支，供 improve 读取。report.md 第一节即结论：
1. 结论：判定；变差的用例数与编号；需要人工判断的项数；未完成的原因；
2. 评测对象：模块、各变体(版本标签、commit、补丁、执行器、模型、评审是否与生成者同一模型)、运行次数、用例数、
   用例集哈希与 evals/ 的树对象编号；
3. 总表：每个变体的总体均值、通过率、总费用、平均耗时；版本对比时附与基线的差值；
4. 变差的用例：基线与候选的均值、方差、差值，从通过变为不通过的评分项与对应运行的输出目录；
5. 逐用例：全部用例的均值、方差、通过率、是否不稳定；
6. 逐评分项：每个评分项在各变体中的通过数，按变化从大到小排列；
7. 需要人工判断：全部 unknown 项及原因；
8. 失败的运行：不通过的运行、原因、输出目录与会话记录路径，失败时读完整记录区分 agent 真错还是评分器误判。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.contracts import validate
from tightrein.domain.enums import EvalVerdict, ScoreResult
from tightrein.evaluation.compare import CaseComparison, ranking
from tightrein.evaluation.stats import CaseStats, RunScore, VariantStats
from tightrein.evaluation.variants import EvaluationPlan
from tightrein.store.files import atomic
from tightrein.store.files.layout import WorkspaceLayout

SCHEMA = "data/eval-report.schema.json"
NO_VERDICT = "不做否决判定(工具与模型对比)"
NONE = "无"


@dataclass(frozen=True)
class EvaluationReport:
    evaluation_id: str
    plan: EvaluationPlan
    manifest_sha256: str
    evals_tree: str
    case_stats: list[CaseStats]
    variant_stats: list[VariantStats]
    comparisons: list[CaseComparison]
    verdict: EvalVerdict | None
    runs: list[RunScore] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    same_model_as_judge: list[str] = field(default_factory=list)
    report_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "kind": "module", "evaluationId": self.evaluation_id, "plan": self.plan.to_dict(),
            "manifestSha256": self.manifest_sha256, "evalsTree": self.evals_tree,
            "caseStats": [item.to_dict() for item in self.case_stats],
            "variantStats": [item.to_dict() for item in self.variant_stats],
            "comparisons": [item.to_dict() for item in self.comparisons],
            "verdict": None if self.verdict is None else self.verdict.value,
        }
        validate.check(SCHEMA, data)
        return data


def _number(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _pair(counts: tuple[int, int] | None) -> str:
    return "—" if counts is None else f"{counts[0]}/{counts[1]}"


def _conclusion(report: EvaluationReport) -> list[str]:
    worse = sorted({item.case_id for item in report.comparisons if item.worse})
    unknown = sum(1 for run in report.runs for item in run.items if item.result is ScoreResult.UNKNOWN)
    verdict = NO_VERDICT if report.verdict is None else f"{report.verdict.label}({report.verdict.value})"
    lines = [f"- 判定：{verdict}",
             f"- 变差的用例：{len(worse)} 个" + (f"({'、'.join(worse)})" if worse else ""),
             f"- 需要人工判断的项：{unknown} 项"]
    lines += [f"- {note}" for note in report.notes]
    return lines


def _subject(report: EvaluationReport) -> list[str]:
    plan = report.plan
    lines = [f"- 模块：{plan.module.value}", f"- 每个用例每个变体运行 {plan.repeats} 次，共 {len(plan.case_ids)} 个用例",
             f"- 用例集哈希：{report.manifest_sha256}", f"- evals/ 树对象：{report.evals_tree}", "",
             "| 变体 | 版本 | commit | 补丁 | 执行器 | 模型 | 评审同模型 |", "|---|---|---|---|---|---|---|"]
    for variant in plan.variants:
        version = variant.version
        commit = "当前工作区" if version.use_worktree else version.commit
        lines.append(f"| {variant.label} | {version.label} | {commit} | {version.patch or NONE} | {variant.runner} | "
                     f"{variant.model or '项目配置'} | {'是' if variant.label in report.same_model_as_judge else '否'} |")
    return lines


def _totals(report: EvaluationReport) -> list[str]:
    ordered = report.variant_stats if report.plan.purpose == "version" else ranking(report.variant_stats)
    baseline = next(item for item in report.variant_stats if item.variant == report.plan.baseline.label)
    lines = ["| 变体 | 总体均值 | 通过率 | 与基线的差值 | 总费用(USD) | 平均耗时(ms) |", "|---|---|---|---|---|---|"]
    for item in ordered:
        delta = "—" if item.variant == baseline.variant else f"{item.mean - baseline.mean:+.3f}"
        lines.append(f"| {item.variant} | {_number(item.mean)} | {_number(item.pass_rate)} | {delta} | "
                     f"{_number(item.cost_usd, 2)} | {_number(item.duration_ms, 0)} |")
    return lines


def _stats_of(report: EvaluationReport, case_id: str, variant: str) -> CaseStats | None:
    return next((item for item in report.case_stats if item.case_id == case_id and item.variant == variant), None)


def _worse(report: EvaluationReport) -> list[str]:
    lines: list[str] = []
    for comparison in (item for item in report.comparisons if item.worse):
        base = _stats_of(report, comparison.case_id, report.plan.baseline.label)
        current = _stats_of(report, comparison.case_id, comparison.variant)
        lines += [f"### {comparison.case_id}({comparison.variant})", "",
                  f"- 基线均值 {_number(comparison.baseline_mean)}，方差 {_number(base.variance if base else None)}；"
                  f"候选均值 {_number(comparison.mean)}，方差 {_number(current.variance if current else None)}；"
                  f"差值 {comparison.delta:+.3f}"]
        dropped = [change for change in comparison.item_changes if change.candidate[0] < change.baseline[0]]
        lines += [f"- {change.item_id}：通过 {_pair(change.baseline)} → {_pair(change.candidate)}" for change in dropped]
        lines += [f"- 输出目录：{run.output_dir}" for run in report.runs
                  if run.case_id == comparison.case_id and run.variant == comparison.variant and not run.passed]
        lines.append("")
    return lines[:-1] if lines else [NONE]


def _cases(report: EvaluationReport) -> list[str]:
    lines = ["| 用例 | 变体 | 均值 | 方差 | 通过率 | 不稳定 |", "|---|---|---|---|---|---|"]
    lines += [f"| {item.case_id} | {item.variant} | {_number(item.mean)} | {_number(item.variance)} | "
              f"{_number(item.pass_rate)} | {'是' if item.unstable else '否'} |" for item in report.case_stats]
    return lines


def _items(report: EvaluationReport) -> list[str]:
    variants = [variant.label for variant in report.plan.variants]
    totals: dict[str, dict[str, list[int]]] = {}
    for item in report.case_stats:
        for item_id, (passes, applicable) in item.item_pass_counts.items():
            counts = totals.setdefault(item_id, {}).setdefault(item.variant, [0, 0])
            counts[0] += passes
            counts[1] += applicable

    def ratio(counts: list[int] | None) -> float:
        return counts[0] / counts[1] if counts and counts[1] else 0.0

    def spread(item_id: str) -> float:
        ratios = [ratio(totals[item_id].get(variant)) for variant in variants]
        return max(ratios) - min(ratios)

    lines = ["| 评分项 | " + " | ".join(variants) + " |", "|---" * (len(variants) + 1) + "|"]
    for item_id in sorted(totals, key=lambda key: (-spread(key), key)):
        cells = [_pair(tuple(totals[item_id][variant])) if variant in totals[item_id] else "—" for variant in variants]
        lines.append(f"| {item_id} | " + " | ".join(cells) + " |")
    return lines


def _unknown(report: EvaluationReport) -> list[str]:
    lines = [f"- {run.variant} / {run.case_id} / 第 {run.attempt} 次 / {item.item_id}：{item.reason}"
             for run in report.runs for item in run.items if item.result is ScoreResult.UNKNOWN]
    return lines or [NONE]


def _failed(report: EvaluationReport) -> list[str]:
    lines = []
    for run in (item for item in report.runs if not item.passed):
        transcripts = "、".join(str(run.output_dir / path) for path in run.transcripts) or NONE
        reason = run.failure or "评分项未全部通过"
        lines.append(f"- {run.variant} / {run.case_id} / 第 {run.attempt} 次：{reason}；输出目录 {run.output_dir}；"
                     f"会话记录 {transcripts}")
    return lines or [NONE]


def render_markdown(report: EvaluationReport) -> str:
    sections = [
        ("结论", _conclusion(report)), ("评测对象", _subject(report)), ("总表", _totals(report)),
        ("变差的用例", _worse(report)), ("逐用例", _cases(report)), ("逐评分项", _items(report)),
        ("需要人工判断", _unknown(report)), ("失败的运行", _failed(report)),
    ]
    lines = [f"# 评测报告 {report.evaluation_id}"]
    for title, body in sections:
        lines += ["", f"## {title}", "", *body]
    return "\n".join(lines) + "\n"


def write(layout: WorkspaceLayout, report: EvaluationReport) -> Path:
    atomic.write_text(layout.eval_report_json(report.evaluation_id),
                      json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n")
    atomic.write_text(layout.eval_report_md(report.evaluation_id), render_markdown(report))
    return layout.eval_report_md(report.evaluation_id)

