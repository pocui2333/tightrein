import json
from dataclasses import replace
from datetime import timedelta

import pytest

from tightrein.contracts import validate
from tightrein.evaluation import manifest
from tightrein.evaluation.errors import EvalCaseInvalid, EvalCaseTampered
from tightrein.observability import events
from tightrein.retrieval.benchmark import (
    BenchmarkComparison,
    CaseRanking,
    RetrievalBenchmarkReport,
    RetrievalMetrics,
    compare_benchmarks,
    evaluate_retrieval,
    load_report,
    render_markdown,
)
from tightrein.retrieval.errors import BaselineMismatch
from tightrein.retrieval.service import KnowledgeService
from tightrein.store.repos import knowledge
from tightrein.vcs.process import VcsProcess


def case(case_id, query, expected, **filters):
    return {"id": case_id, "kind": "retrieval", "query": query, "filters": filters, "expected": expected,
            "source": "R-20261001-010000-triage"}


CASES = [
    case("E-0001", "公司过滤", ["DP-0001"]),
    case("E-0002", "过滤", ["DP-0002", "DP-0003"]),
    case("E-0003", "软删除", ["DP-0004"]),
    case("E-0004", "公司", ["DP-0099"]),
    case("E-0005", "不存在的词", ["DP-0003"]),
]


@pytest.fixture
def bench(world, repos):
    root = world.tool.root
    repos.git(root, "init", "-q", "-b", "main")
    repos.write(root, ".gitignore", "workspaces/*/data/\n")
    world.entry("DP-0001", "company", "公司过滤", "列表接口按公司过滤")
    world.entry("DP-0002", "department", "部门", "部门过滤缺失")
    world.entry("DP-0003", "paging", "分页从零开始", "分页参数")
    world.entry("DP-0004", "old", "旧的软删除", "旧条目", status="superseded", superseded_by="DP-0005")
    world.entry("DP-0005", "soft-delete", "软删除", "软删除查询")
    path = world.layout.retrieval_cases()
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in CASES), encoding="utf-8")
    world.layout.eval_manifest().write_text(json.dumps({"schemaVersion": 1, "cases": manifest.compute(world.layout)}),
                                            encoding="utf-8")
    repos.commit(root, "test: 检索评测")
    return VcsProcess(environ=repos.environ)


def service(world):
    return KnowledgeService(world.layout, world.conn, world.clock, world.tracer)


def test_metrics_follow_the_hand_computed_values(world, bench):
    report = evaluate_retrieval(service(world), bench, world.clock)
    assert report.evaluation_id == "EV-20261005-030000"
    assert [(item.case_id, item.rank, item.hits) for item in report.cases] == [
        ("E-0001", 1, ["DP-0001"]),
        ("E-0002", 2, ["DP-0001", "DP-0002"]),
        ("E-0003", 1, ["DP-0005"]),
        ("E-0005", None, []),
    ]
    metrics = report.metrics
    assert (metrics.case_count, metrics.skipped_cases) == (4, [("E-0004", "期望的条目 DP-0099 在索引中不存在")])
    assert metrics.recall_at_5 == pytest.approx((1 + 0.5 + 1 + 0) / 4)
    assert metrics.recall_at_10 == pytest.approx((1 + 0.5 + 1 + 0) / 4)
    assert metrics.mrr == pytest.approx((1 + 0.5 + 1 + 0) / 4)
    assert report.cases_sha256 == manifest.file_sha256(world.layout.retrieval_cases())


def test_reports_are_written_and_benchmark_queries_leave_no_trace(world, bench):
    report = evaluate_retrieval(service(world), bench, world.clock)
    directory = world.layout.eval_output_dir(report.evaluation_id)
    data = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    assert validate.validate("data/eval-report.schema.json", data) == []
    assert data["skippedCases"] == [{"caseId": "E-0004", "reason": "期望的条目 DP-0099 在索引中不存在"}]
    text = (directory / "report.md").read_text(encoding="utf-8")
    assert text.startswith("# 检索评测 EV-20261005-030000\n\n## 结论\n\nrecall@5 0.625，recall@10 0.625，MRR 0.625；"
                           "参与计算 4 个用例，跳过 1 个。\n")
    assert "| E-0005 | 未命中 | 无 |" in text and "- E-0004：期望的条目 DP-0099 在索引中不存在" in text
    assert load_report(world.layout, report.evaluation_id).metrics == report.metrics
    logged = events.read(world.layout.events_log(world.clock.now().date()))
    assert [event.attributes["operation"] for event in logged if event.agent == "kb"] == ["sync"]
    assert all(record.hits == 0 for record in knowledge.find(world.conn))


def report_with(evaluation_id, sha, recall, ranks):
    cases = [CaseRanking(case_id, rank, []) for case_id, rank in ranks.items()]
    return RetrievalBenchmarkReport(evaluation_id, sha, RetrievalMetrics(recall, recall, recall, len(cases)), cases)


def test_comparison_rejects_any_drop_and_lists_worse_cases():
    baseline = report_with("EV-20261001-010000", "a" * 64, 0.8, {"E-0001": 1, "E-0002": 2, "E-0003": None})
    better = report_with("EV-20261005-030000", "a" * 64, 0.9, {"E-0001": 1, "E-0002": 1, "E-0003": 4})
    worse = report_with("EV-20261005-030000", "a" * 64, 0.7, {"E-0001": None, "E-0002": 3, "E-0003": None})
    assert compare_benchmarks(baseline, better) == BenchmarkComparison("EV-20261001-010000", True, [], [])
    assert compare_benchmarks(baseline, worse) == BenchmarkComparison(
        "EV-20261001-010000", False, ["E-0001", "E-0002"], ["recall@5", "recall@10", "MRR"])
    with pytest.raises(BaselineMismatch, match="用例集与本次不同"):
        compare_benchmarks(baseline, replace(better, cases_sha256="b" * 64))
    text = render_markdown(replace(worse, baseline=compare_benchmarks(baseline, worse)))
    assert "与基线 EV-20261001-010000 比较：不采纳：recall@5、recall@10、MRR 下降。\n排名变差的用例：E-0001、E-0002。" in text


def test_a_baseline_run_is_compared(world, bench):
    first = evaluate_retrieval(service(world), bench, world.clock)
    world.clock.advance(timedelta(minutes=1))
    second = evaluate_retrieval(service(world), bench, world.clock, baseline_id=first.evaluation_id)
    assert second.baseline == BenchmarkComparison(first.evaluation_id, True, [], [])
    assert json.loads((world.layout.eval_output_dir(second.evaluation_id) / "report.json").read_text(
        encoding="utf-8"))["baseline"] == {"evaluationId": first.evaluation_id, "adopted": True, "worseCases": []}
    with pytest.raises(BaselineMismatch, match="不存在"):
        evaluate_retrieval(service(world), bench, world.clock, baseline_id="EV-20200101-000000")


def test_tampered_or_missing_cases_stop_before_any_query(world, bench, repos):
    with world.layout.retrieval_cases().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(case("E-0006", "新增", ["DP-0001"]), ensure_ascii=False) + "\n")
    with pytest.raises(EvalCaseTampered):
        evaluate_retrieval(service(world), bench, world.clock)
    world.layout.retrieval_cases().unlink()
    world.layout.eval_manifest().write_text(json.dumps({"schemaVersion": 1, "cases": {}}), encoding="utf-8")
    repos.commit(world.tool.root, "test: 删除检索用例")
    with pytest.raises(EvalCaseInvalid, match="文件不存在或没有用例"):
        evaluate_retrieval(service(world), bench, world.clock)
    assert not world.layout.eval_output_dir("EV-20261005-030000").exists()
