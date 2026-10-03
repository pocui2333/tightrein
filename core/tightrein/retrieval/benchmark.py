"""检索评测(architecture/03 1.9，design 16.8)。

1. 用 evaluation.cases 校验 evals/ 与 manifest.json 一致、没有未提交的改动、cases.jsonl 每行合格；
2. 先显式同步一次索引，再对每个用例以 limit=10 执行检索；检索评测不计命中，也不写 kb 的检索事件(这些事件是
   挑选评测用例的来源，不能被评测自己污染)；
3. 期望编号已被取代时沿 supersededBy 替换为当前有效的编号；期望编号在索引中不存在的，该用例跳过并记下原因；
4. recall@k = 前 k 条中命中的期望编号数 ÷ 期望编号数，k 取 5 与 10；MRR = 第一个命中的期望编号排名的倒数，
   前 10 条中没有命中记 0；三项都对参与计算的用例取平均；
5. 报告写入 data/evals/<评测编号>/：report.json 符合 data/eval-report.schema.json 的 retrieval 分支，report.md 先写结论；
6. 给出基线时，两份报告的用例集哈希必须相同；任何一项指标下降即判为不采纳，并列出排名变差的用例。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from tightrein.contracts import validate
from tightrein.domain import ids
from tightrein.domain.clock import Clock
from tightrein.evaluation.cases import RetrievalCase, verify_cases
from tightrein.evaluation.errors import CaseProblem, EvalCaseInvalid
from tightrein.evaluation.stats import mean
from tightrein.retrieval import search
from tightrein.retrieval.errors import BaselineMismatch
from tightrein.retrieval.service import KnowledgeService
from tightrein.store.files import atomic
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import knowledge
from tightrein.vcs.process import VcsProcess

REPORT_SCHEMA = "data/eval-report.schema.json"
CASES_KEY = "retrieval/cases.jsonl"
TOP = 10
RECALL_KS = (5, 10)
MISSING_RANK = TOP + 1


@dataclass(frozen=True)
class CaseRanking:
    case_id: str
    rank: int | None
    hits: list[str]


@dataclass(frozen=True)
class RetrievalMetrics:
    recall_at_5: float
    recall_at_10: float
    mrr: float
    case_count: int
    skipped_cases: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class BenchmarkComparison:
    baseline_id: str
    adopted: bool
    worse_cases: list[str]
    dropped_metrics: list[str]


@dataclass(frozen=True)
class RetrievalBenchmarkReport:
    evaluation_id: str
    cases_sha256: str
    metrics: RetrievalMetrics
    cases: list[CaseRanking]
    baseline: BenchmarkComparison | None = None

    def to_dict(self) -> dict[str, Any]:
        metrics = self.metrics
        data = {
            "kind": "retrieval", "evaluationId": self.evaluation_id, "casesSha256": self.cases_sha256,
            "metrics": {"recallAt5": metrics.recall_at_5, "recallAt10": metrics.recall_at_10, "mrr": metrics.mrr,
                        "caseCount": metrics.case_count},
            "cases": [{"caseId": case.case_id, "rank": case.rank, "hits": case.hits} for case in self.cases],
            "skippedCases": [{"caseId": case_id, "reason": reason} for case_id, reason in metrics.skipped_cases],
            "baseline": None if self.baseline is None else {
                "evaluationId": self.baseline.baseline_id, "adopted": self.baseline.adopted,
                "worseCases": self.baseline.worse_cases},
        }
        validate.check(REPORT_SCHEMA, data)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RetrievalBenchmarkReport:
        validate.check(REPORT_SCHEMA, data)
        metrics = data["metrics"]
        return cls(
            data["evaluationId"], data["casesSha256"],
            RetrievalMetrics(metrics["recallAt5"], metrics["recallAt10"], metrics["mrr"], metrics["caseCount"],
                             [(item["caseId"], item["reason"]) for item in data["skippedCases"]]),
            [CaseRanking(item["caseId"], item["rank"], item["hits"]) for item in data["cases"]],
        )


def current_id(conn: sqlite3.Connection, entry_id: str) -> str | None:
    """沿 supersededBy 追到当前有效的编号；编号不存在或链断开时为空。"""
    seen = set()
    record = knowledge.get(conn, entry_id)
    while record is not None and record.superseded_by is not None and record.id not in seen:
        seen.add(record.id)
        record = knowledge.get(conn, record.superseded_by)
    return None if record is None else record.id


def _recall(expected: Sequence[str], hits: Sequence[str], k: int) -> float:
    return len(set(expected) & set(hits[:k])) / len(expected)


def run_benchmark(service: KnowledgeService, cases: Sequence[RetrievalCase], cases_sha256: str,
                  evaluation_id: str) -> RetrievalBenchmarkReport:
    rankings: list[CaseRanking] = []
    skipped: list[tuple[str, str]] = []
    recalls: dict[int, list[float]] = {k: [] for k in RECALL_KS}
    reciprocal: list[float] = []
    for case in cases:
        expected = []
        for entry_id in case.expected:
            resolved = current_id(service.conn, entry_id)
            if resolved is None:
                skipped.append((case.id, f"期望的条目 {entry_id} 在索引中不存在"))
                break
            expected.append(resolved)
        else:
            filters = replace(case.filters, limit=TOP)
            hits = [hit.id for hit in search.search(service.conn, service.sources, case.query, filters,
                                                    **service.search_options())]
            rank = next((position for position, hit in enumerate(hits, start=1) if hit in expected), None)
            rankings.append(CaseRanking(case.id, rank, hits))
            for k in RECALL_KS:
                recalls[k].append(_recall(expected, hits, k))
            reciprocal.append(0.0 if rank is None else 1.0 / rank)
    metrics = RetrievalMetrics(mean(recalls[5]), mean(recalls[10]), mean(reciprocal), len(rankings), skipped)
    return RetrievalBenchmarkReport(evaluation_id, cases_sha256, metrics, rankings)


def compare_benchmarks(baseline: RetrievalBenchmarkReport,
                       current: RetrievalBenchmarkReport) -> BenchmarkComparison:
    if baseline.cases_sha256 != current.cases_sha256:
        raise BaselineMismatch(
            f"基线 {baseline.evaluation_id} 使用的用例集与本次不同(用例集被扩充或修改过)，不能比较；"
            "以本次报告作为此后的基线")
    dropped = [name for name, before, after in (
        ("recall@5", baseline.metrics.recall_at_5, current.metrics.recall_at_5),
        ("recall@10", baseline.metrics.recall_at_10, current.metrics.recall_at_10),
        ("MRR", baseline.metrics.mrr, current.metrics.mrr),
    ) if after < before]
    before = {case.case_id: case.rank or MISSING_RANK for case in baseline.cases}
    worse = [case.case_id for case in current.cases
             if case.case_id in before and (case.rank or MISSING_RANK) > before[case.case_id]]
    return BenchmarkComparison(baseline.evaluation_id, not dropped, worse, dropped)


def render_markdown(report: RetrievalBenchmarkReport) -> str:
    metrics = report.metrics
    lines = [f"# 检索评测 {report.evaluation_id}", "", "## 结论", "",
             f"recall@5 {metrics.recall_at_5:.3f}，recall@10 {metrics.recall_at_10:.3f}，MRR {metrics.mrr:.3f}；"
             f"参与计算 {metrics.case_count} 个用例，跳过 {len(metrics.skipped_cases)} 个。"]
    if report.baseline is not None:
        comparison = report.baseline
        verdict = "采纳" if comparison.adopted else f"不采纳：{'、'.join(comparison.dropped_metrics)} 下降"
        lines += ["", f"与基线 {comparison.baseline_id} 比较：{verdict}。"]
        if comparison.worse_cases:
            lines.append(f"排名变差的用例：{'、'.join(comparison.worse_cases)}。")
    lines += ["", f"用例集哈希：{report.cases_sha256}", "", "## 逐用例", "", "| 用例 | 首个命中排名 | 返回的编号 |",
              "|---|---|---|"]
    lines += [f"| {case.case_id} | {case.rank if case.rank is not None else '未命中'} | {'、'.join(case.hits) or '无'} |"
              for case in report.cases]
    if metrics.skipped_cases:
        lines += ["", "## 跳过的用例", ""]
        lines += [f"- {case_id}：{reason}" for case_id, reason in metrics.skipped_cases]
    return "\n".join(lines) + "\n"


def write_report(layout: WorkspaceLayout, report: RetrievalBenchmarkReport) -> Path:
    atomic.write_text(layout.eval_report_json(report.evaluation_id),
                      json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n")
    atomic.write_text(layout.eval_report_md(report.evaluation_id), render_markdown(report))
    return layout.eval_report_md(report.evaluation_id)


def load_report(layout: WorkspaceLayout, evaluation_id: str) -> RetrievalBenchmarkReport:
    path = layout.eval_report_json(evaluation_id)
    if not path.is_file():
        raise BaselineMismatch(f"基线 {evaluation_id} 的报告 {path} 不存在")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("kind") != "retrieval":
        raise BaselineMismatch(f"{evaluation_id} 不是检索评测的报告")
    return RetrievalBenchmarkReport.from_dict(data)


def evaluate_retrieval(service: KnowledgeService, process: VcsProcess, clock: Clock,
                       baseline_id: str | None = None) -> RetrievalBenchmarkReport:
    """kb eval 背后的函数：校验用例、同步索引、计算指标、与基线比较并写报告。"""
    layout = service.layout
    verified = verify_cases(layout, process)
    if not verified.retrieval_cases:
        raise EvalCaseInvalid([CaseProblem(CASES_KEY, "文件不存在或没有用例")])
    baseline = None if baseline_id is None else load_report(layout, baseline_id)
    service.sync()
    report = run_benchmark(service, verified.retrieval_cases, verified.case_hashes[CASES_KEY],
                           ids.eval_id(clock.now()))
    if baseline is not None:
        report = replace(report, baseline=compare_benchmarks(baseline, report))
    write_report(layout, report)
    return report
