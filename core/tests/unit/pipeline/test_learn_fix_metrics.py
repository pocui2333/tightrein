from datetime import timedelta

from learn_world import WEEK, WEEK_START, ZONE, closed_fixed, fix_outputs, issue_event, make_learn_world, save_handoff, triaged
from pipeline_world import NOW

from tightrein.domain.enums import (
    IssueEvent,
    OperationExecutor,
    OperationKind,
    OperationStatus,
    RunnerStatus,
    RunStage,
    Stage,
    TriageOutcome,
)
from tightrein.pipeline.learn.steps import fix_metrics, metrics, weeks
from tightrein.pipeline.learn.steps.metrics import MetricContext
from tightrein.store.repos import pending_operations, stage_yield
from tightrein.store.repos.pending_operations import PendingOperationRecord
from tightrein.store.repos.stage_yield import StageYieldRecord


def context(world):
    return MetricContext(world.conn, world.layout, world.config, weeks.week_of(WEEK, ZONE), NOW, ZONE)


def values(found, metric):
    return {value.dimension: (value.value, value.numerator, value.denominator) for value in found
            if value.metric == metric}


def round_(passed):
    return {"round": 1, "checksPassed": passed, "reviews": [], "blockerCategories": [], "risk": None, "failures": [],
            "discardedFindings": []}


def call(world, run_id, issue_id, role="fix-executor", model="opus", cost=1.0, stage=Stage.FIX):
    stage_yield.append(world.conn, StageYieldRecord(run_id, stage, role, issue_id, 1, RunnerStatus.OK, NOW,
                                                    cost_usd=cost, tool="claude", model=model))


def operation(world, number, kind, status, subject="0007", decided=NOW):
    pending_operations.save(world.conn, PendingOperationRecord(
        f"OP-000{number}", Stage.FIX, subject, kind, OperationExecutor.VCS, "i", True, f"k{number}", 1, status,
        NOW - timedelta(days=1), decided_at=decided))


def test_first_pass_by_lane_and_model_uses_the_first_round_of_the_first_fix(tmp_path):
    world = make_learn_world(tmp_path)
    save_handoff(world, RunStage.FIX, "0007", {**fix_outputs("0007", rounds=[round_(True)]), "lane": "fast"}, "R-20261001-030000-fix")
    save_handoff(world, RunStage.FIX, "0008", {**fix_outputs("0008", rounds=[round_(False), round_(True)]),
                                               "lane": "standard"}, "R-20261002-030000-fix")
    save_handoff(world, RunStage.FIX, "0008", {**fix_outputs("0008", rounds=[round_(True)]), "lane": "standard"},
                 "R-20261003-030000-fix", attempt=2)
    save_handoff(world, RunStage.FIX, "0009", {**fix_outputs("0009", rounds=[round_(True)]), "lane": "fast"}, "R-20261004-030000-fix",
                 at=WEEK_START - timedelta(days=1))
    call(world, "R-20261001-030000-fix", "0007", model="opus")
    call(world, "R-20261002-030000-fix", "0008", role="fix-session", model="sonnet")
    found = values(fix_metrics.first_pass(context(world)), "first-pass")
    assert found == {"all": (0.5, 1, 2), "lane=fast": (1.0, 1, 1), "lane=standard": (0.0, 0, 1),
                     "model=opus": (1.0, 1, 1), "model=sonnet": (0.0, 0, 1)}


def test_fix_cost_and_revert_rate_by_lane(tmp_path):
    world = make_learn_world(tmp_path)
    for issue_id, lane in (("0007", "fast"), ("0008", "standard")):
        save_handoff(world, RunStage.FIX, issue_id, {**fix_outputs(issue_id), "lane": lane}, "R-20261001-030000-fix")
        closed_fixed(world, issue_id)
    call(world, "R-20261001-030000-fix", "0007", cost=0.5)
    call(world, "R-20261001-030000-fix", "0007", role="fix-reviewer-light", cost=0.25)
    call(world, "R-20261001-030000-fix", "0008", cost=4.0)
    call(world, "R-20261001-030000-fix", "0008", cost=9.0, stage=Stage.TRIAGE)
    assert values(fix_metrics.fix_cost(context(world)), "fix-cost-usd") == {
        "all": (2.375, 4.75, 2), "lane=fast": (0.75, 0.75, 1), "lane=standard": (4.0, 4.0, 1)}
    operation(world, 1, OperationKind.REVERT_PULL_REQUEST, OperationStatus.EXECUTED, "0008")
    operation(world, 2, OperationKind.REVERT_PULL_REQUEST, OperationStatus.REJECTED, "0007")
    assert values(fix_metrics.revert_rate(context(world)), "revert-rate") == {
        "all": (0.5, 1, 2), "lane=fast": (0.0, 0, 1), "lane=standard": (1.0, 1, 1)}


def test_user_corrections_count_each_kind_within_the_week(tmp_path):
    world = make_learn_world(tmp_path)
    operation(world, 1, OperationKind.FIX_PLAN, OperationStatus.REJECTED)
    operation(world, 2, OperationKind.FIX_PLAN, OperationStatus.REJECTED, decided=WEEK_START - timedelta(days=1))
    operation(world, 3, OperationKind.REVERT_PULL_REQUEST, OperationStatus.CONFIRMED)
    issue_event(world, "0009", IssueEvent.USER_CLOSED)
    triaged(world, "P-0001", outcome=TriageOutcome.OVERRIDDEN, outcome_at=NOW)
    found = values(fix_metrics.user_corrections(context(world)), "user-corrections")
    assert found == {"all": (4, None, None), "kind=plan-rejected": (1, None, None), "kind=issue-closed": (1, None, None),
                     "kind=reverted": (1, None, None), "kind=triage-overridden": (1, None, None)}


def test_the_new_metrics_are_registered_by_stage(tmp_path):
    world = make_learn_world(tmp_path)
    found, errors = metrics.compute(context(world), Stage.FIX)
    assert errors == []
    assert {"first-pass", "fix-cost-usd"} <= {value.metric for value in found}
    assert "user-corrections" in {value.metric for value in metrics.compute(context(world))[0]}
