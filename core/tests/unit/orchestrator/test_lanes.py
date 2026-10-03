import pytest

from tightrein.config.project import core_config
from tightrein.domain.enums import Lane, ReviewMode, SizeTier, TaskType
from tightrein.orchestrator.policy import lanes
from tightrein.orchestrator.policy.lanes import ReproMode

CONFIG = core_config()


@pytest.mark.parametrize("task_type,tier,expected", [
    (TaskType.BUG, SizeTier.MICRO, Lane.FAST),
    (TaskType.FRONTEND, SizeTier.SMALL, Lane.FAST),
    (TaskType.BUG, SizeTier.MEDIUM, Lane.STANDARD),
    (TaskType.REFACTOR, SizeTier.LARGE, Lane.LARGE),
    (TaskType.SECURITY, SizeTier.MICRO, Lane.STANDARD),
    (TaskType.DATA, SizeTier.SMALL, Lane.STANDARD),
    (TaskType.DEPENDENCY, SizeTier.MICRO, Lane.STANDARD),
    (TaskType.FEATURE, None, Lane.STANDARD),
    (TaskType.BUG, SizeTier.OVERSIZE, None),
])
def test_the_default_lane_table(task_type, tier, expected):
    assert lanes.route(CONFIG, task_type, tier) is expected


def test_tiers_come_from_the_thresholds():
    assert [lanes.tier_of(CONFIG, files, lines) for files, lines in ((1, 30), (1, 31), (3, 100), (10, 500), (30, 3000),
                                                                     (31, 1))] == [
        SizeTier.MICRO, SizeTier.SMALL, SizeTier.SMALL, SizeTier.MEDIUM, SizeTier.LARGE, SizeTier.OVERSIZE]


def test_scouting_repro_mode_and_review_depth():
    assert not lanes.needs_scout(CONFIG, TaskType.BUG, has_root_cause=True, has_scope=True)
    assert lanes.needs_scout(CONFIG, TaskType.BUG, has_root_cause=False, has_scope=True)
    assert lanes.needs_scout(CONFIG, TaskType.DATA, has_root_cause=True, has_scope=True)
    assert [lanes.repro_mode(CONFIG, item) for item in (TaskType.DOCS_CONFIG, TaskType.SECURITY, TaskType.BUG)] == [
        ReproMode.SKIP, ReproMode.INDEPENDENT, ReproMode.SAME_SESSION]
    assert lanes.expects_pass(CONFIG, TaskType.REFACTOR) and not lanes.expects_pass(CONFIG, TaskType.FEATURE)
    fast = dict(checks_passed=True, high_risk=True)
    assert lanes.review_modes(CONFIG, Lane.FAST, actual_tier=SizeTier.MICRO, **fast) == ()
    assert lanes.review_modes(CONFIG, Lane.FAST, actual_tier=SizeTier.SMALL, **fast) == (ReviewMode.LIGHT,)
    assert lanes.review_modes(CONFIG, Lane.STANDARD, actual_tier=SizeTier.MICRO, checks_passed=True,
                              high_risk=False) == (ReviewMode.LIGHT,)
    assert lanes.review_modes(CONFIG, Lane.STANDARD, actual_tier=SizeTier.MICRO, **fast) == (
        ReviewMode.LIGHT, ReviewMode.DEEP)


def test_projects_override_the_table(make_config):
    config = make_config(fix={"lanes": {"default": {"micro": "standard", "small": "standard", "medium": "standard",
                                                    "large": "large"}}})
    assert lanes.route(config, TaskType.BUG, SizeTier.MICRO) is Lane.STANDARD
    assert lanes.route(config, TaskType.SECURITY, SizeTier.MICRO) is Lane.STANDARD
