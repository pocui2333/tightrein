from datetime import timedelta

from learn_world import WEEK, ZONE, make_learn_world
from pipeline_world import NOW

from tightrein.domain.enums import OperationExecutor, OperationKind, OperationStatus, Stage, SuggestionKind
from tightrein.pipeline.learn.steps import controls, metrics, weeks
from tightrein.pipeline.learn.steps.metric_base import MetricContext, MetricValue
from tightrein.store.repos import pending_operations
from tightrein.store.repos.pending_operations import PendingOperationRecord


def context(world):
    return MetricContext(world.conn, world.layout, world.config, weeks.week_of(WEEK, ZONE), NOW, ZONE)


def first_pass(model, passed, total):
    return MetricValue.ratio("first-pass", f"model={model}", passed, total)


def history(world, *weekly):
    """weekly 为此前各周(旧到新)的 (通过数, 样本数)。"""
    for offset, (passed, total) in enumerate(reversed(weekly), start=1):
        metrics.save_snapshots(world.conn, WEEK - timedelta(weeks=offset), [first_pass("opus", passed, total)], NOW)


def rejected_plans(world, count):
    for number in range(1, count + 1):
        pending_operations.save(world.conn, PendingOperationRecord(
            f"OP-000{number}", Stage.FIX, f"000{number}", OperationKind.FIX_PLAN, OperationExecutor.VCS, "i", True,
            f"k{number}", 1, OperationStatus.REJECTED, NOW - timedelta(days=1), decided_at=NOW))


def test_a_model_low_for_consecutive_weeks_gets_one_upgrade_suggestion(tmp_path):
    world = make_learn_world(tmp_path)
    history(world, (2, 6), (1, 5))
    (draft,) = controls.drafts(context(world), WEEK, [first_pass("opus", 2, 5), first_pass("sonnet", 1, 5)])
    assert (draft.kind, draft.subject, draft.evidence["rates"]) == (
        SuggestionKind.CONTROL, "first-pass:opus", [2 / 6, 0.2, 0.4])
    assert "routes.fix.executor" in draft.document.apply
    assert "tightrein admin eval run" in draft.document.apply and draft.document.recommended is True


def test_short_history_small_samples_or_a_good_week_give_no_suggestion(tmp_path):
    world = make_learn_world(tmp_path)
    history(world, (1, 5))
    assert controls.drafts(context(world), WEEK, [first_pass("opus", 1, 5)]) == []
    history(world, (1, 4), (1, 5))
    assert controls.drafts(context(world), WEEK, [first_pass("opus", 1, 5)]) == []
    history(world, (1, 5), (1, 5))
    assert controls.drafts(context(world), WEEK, [first_pass("opus", 4, 5)]) == []


def test_frequent_corrections_of_an_automatic_gate_suggest_asking_the_user(tmp_path):
    world = make_learn_world(tmp_path, gates={"plan-confirm": "auto"})
    rejected_plans(world, 3)
    (draft,) = controls.drafts(context(world), WEEK, [])
    assert (draft.subject, draft.evidence["subjects"]) == ("gate:plan-confirm", ["0001", "0002", "0003"])
    assert "gates: {plan-confirm: user}" in draft.document.apply


def test_corrections_of_a_user_gate_or_below_the_limit_give_no_suggestion(tmp_path):
    world = make_learn_world(tmp_path)
    rejected_plans(world, 3)
    assert controls.drafts(context(world), WEEK, []) == []
    world = make_learn_world(tmp_path / "other", gates={"plan-confirm": "auto"})
    rejected_plans(world, 2)
    assert controls.drafts(context(world), WEEK, []) == []
