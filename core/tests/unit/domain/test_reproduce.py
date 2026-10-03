import pytest

from tightrein.domain.enums import Probe, ProblemEvent, ReproduceStrategy
from tightrein.domain.reproduce import judge_replays, strategy


@pytest.mark.parametrize("probe,check,expected", [
    (Probe.API_FUZZ, "not_a_server_error", ReproduceStrategy.REPLAY),
    (Probe.API_FUZZ, "unauthorized_role_access", ReproduceStrategy.IMMEDIATE),
    (Probe.API_FUZZ, "unsupported_method", ReproduceStrategy.IMMEDIATE),
    (Probe.API_FUZZ, "status_code_conformance", ReproduceStrategy.IMMEDIATE),
    (Probe.API_FUZZ, "max_response_time", ReproduceStrategy.IMMEDIATE),
    (Probe.PLATFORM_ERRORS, "error", ReproduceStrategy.IMMEDIATE),
    (Probe.ALERTS, "business-alert", ReproduceStrategy.IMMEDIATE),
    (Probe.PROJECT_PROBE, "daily-import", ReproduceStrategy.IMMEDIATE),
    (Probe.STATIC, "missing-owner-check", ReproduceStrategy.IMMEDIATE),
    (Probe.INCIDENTAL, "incidental", ReproduceStrategy.IMMEDIATE),
])
def test_strategy(probe, check, expected):
    assert strategy(probe, check) is expected


@pytest.mark.parametrize("results,expected", [
    ([True, False], ProblemEvent.REPRODUCED),
    ([False, True], ProblemEvent.REPRODUCED),
    ([True, None], ProblemEvent.REPRODUCED),
    ([False, False], ProblemEvent.NOT_REPRODUCED),
    ([False, None], None),
    ([None, None], None),
])
def test_judge_replays(results, expected):
    assert judge_replays(results) is expected


def test_judge_replays_checks_attempt_count():
    with pytest.raises(ValueError):
        judge_replays([True])
    assert judge_replays([False, False, True], attempts=3) is ProblemEvent.REPRODUCED
