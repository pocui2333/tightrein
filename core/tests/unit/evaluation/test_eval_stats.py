from pathlib import Path

import pytest

from tightrein.domain.enums import EvalVerdict, RunnerStatus, ScoreMethod, ScoreResult, Stage
from tightrein.evaluation import compare, stats
from tightrein.evaluation.scorers.base import ItemResult
from tightrein.evaluation.scoring import summarize
from tightrein.evaluation.stats import RunScore
from tightrein.evaluation.variants import VersionSpec, tool_model_plan, version_plan
from tightrein.runner.result import Usage

PLAN = version_plan(Stage.TRIAGE, VersionSpec("IP-0003", use_worktree=True), "claude")
VARIANTS = ["baseline", "IP-0003"]


def run(case_id, variant, attempt, *results, cost=0.5, duration=1000):
    items = [ItemResult(f"triage.item-{index}", ScoreMethod.CODE, ScoreResult(value), "")
             for index, value in enumerate(results)]
    score, passed = summarize(items)
    return RunScore(case_id, variant, attempt, RunnerStatus.OK, items, score, passed, Usage(100, 10, None, cost),
                    duration, Path("/out") / variant / case_id / str(attempt))


def runs_for(case_id, variant, *attempts):
    return [run(case_id, variant, number, *results) for number, results in enumerate(attempts, start=1)]


def test_hand_computed_case_statistics():
    runs = runs_for("E-0001", "baseline", ("pass", "pass"), ("pass", "fail"), ("fail", "fail", "not-applicable"))
    [case] = stats.case_stats(runs, VARIANTS, ["E-0001"])
    assert case.scores == [1.0, 0.5, 0.0]
    assert case.mean == pytest.approx(0.5)
    assert case.variance == pytest.approx(((0.5) ** 2 + 0 + (0.5) ** 2) / 2)
    assert case.pass_rate == pytest.approx(1 / 3)
    assert case.item_pass_counts == {"triage.item-0": (2, 3), "triage.item-1": (1, 3)}
    assert case.unstable is True
    assert stats.sample_variance([0.7]) == 0.0 and stats.mean([]) == 0.0


def test_variant_totals():
    runs = runs_for("E-0001", "baseline", ("pass",), ("pass",), ("fail",)) + \
        runs_for("E-0002", "baseline", ("pass",), ("pass",), ("pass",))
    case_stats = stats.case_stats(runs, VARIANTS, ["E-0001", "E-0002"])
    [baseline, candidate] = stats.variant_stats(runs, case_stats, VARIANTS)
    assert (baseline.mean, baseline.pass_rate, baseline.cost_usd, baseline.duration_ms) == (
        pytest.approx((2 / 3 + 1) / 2), pytest.approx(5 / 6), pytest.approx(3.0), 1000)
    assert (candidate.mean, candidate.pass_rate, candidate.cost_usd, candidate.duration_ms) == (0.0, 0.0, None, None)


def verdict_for(baseline, candidate, complete=True):
    runs = runs_for("E-0001", "baseline", *baseline) + runs_for("E-0001", "IP-0003", *candidate)
    case_stats = stats.case_stats(runs, VARIANTS, ["E-0001"])
    comparisons = compare.compare(case_stats, "baseline")
    return compare.verdict(PLAN, comparisons, runs, complete), comparisons


@pytest.mark.parametrize("candidate, expected", [
    ((("pass", "fail"), ("pass", "pass"), ("pass", "pass")), EvalVerdict.REJECT),
    ((("pass", "pass"), ("pass", "unknown"), ("pass", "pass")), EvalVerdict.NEEDS_REVIEW),
    ((("pass", "pass"), ("pass", "pass"), ("pass", "pass")), EvalVerdict.PASS),
])
def test_version_verdicts(candidate, expected):
    stable = (("pass", "pass"), ("pass", "pass"), ("pass", "pass"))
    assert verdict_for(stable, candidate)[0] is expected


def test_a_newly_unstable_case_needs_review_even_without_a_lower_mean():
    baseline = (("pass", "pass"), ("pass", "pass"), ("pass", "pass"))
    candidate = (("pass", "unknown"), ("pass", "pass"), ("pass", "pass"))
    result, comparisons = verdict_for(baseline, candidate)
    assert result is EvalVerdict.NEEDS_REVIEW and comparisons[0].delta == 0.0 and comparisons[0].unstable is True


def test_comparisons_list_deltas_and_item_changes():
    baseline = (("pass", "fail"), ("pass", "fail"), ("pass", "fail"))
    candidate = (("pass", "pass"), ("pass", "pass"), ("pass", "fail"))
    result, [comparison] = verdict_for(baseline, candidate)
    assert result is EvalVerdict.PASS
    assert (comparison.baseline_mean, comparison.mean) == (pytest.approx(0.5), pytest.approx(5 / 6))
    assert comparison.delta == pytest.approx(1 / 3)
    assert [change.to_dict() for change in comparison.item_changes] == [
        {"itemId": "triage.item-1", "baseline": [0, 3], "candidate": [2, 3]}]
    assert verdict_for(baseline, candidate, complete=False)[0] is EvalVerdict.INCOMPLETE


def test_tool_and_model_comparisons_rank_without_a_verdict():
    plan = tool_model_plan(Stage.TRIAGE, ["claude", "codex"])
    runs = runs_for("E-0001", "claude+default", ("pass",), ("fail",), ("pass",)) + \
        runs_for("E-0001", "codex+default", ("pass",), ("pass",), ("pass",))
    labels = [variant.label for variant in plan.variants]
    case_stats = stats.case_stats(runs, labels, ["E-0001"])
    comparisons = compare.compare(case_stats, labels[0])
    assert compare.verdict(plan, comparisons, runs, True) is None
    assert compare.verdict(plan, comparisons, runs, False) is EvalVerdict.INCOMPLETE
    ranked = compare.ranking(stats.variant_stats(runs, case_stats, labels))
    assert [item.variant for item in ranked] == ["codex+default", "claude+default"]


def test_run_scores_round_trip():
    original = run("E-0001", "baseline", 2, "pass", "unknown")
    assert RunScore.from_dict(original.to_dict()) == original
