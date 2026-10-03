from datetime import timedelta

import pytest

from pipeline_world import NOW

from tightrein.domain.enums import CheckResult, Probe
from tightrein.pipeline.verify.steps import confirm
from tightrein.pipeline.verify.steps.verdict import Item


def entry(method, result):
    return {"method": method, "result": result.value}


def test_observation_regresses_waits_or_passes():
    assert confirm.observe(NOW + timedelta(hours=1), NOW, NOW, 48)[0] is CheckResult.FAIL
    assert confirm.observe(NOW, NOW, NOW + timedelta(hours=1), 48)[0] is CheckResult.UNVERIFIED
    assert confirm.observe(NOW, NOW, NOW + timedelta(hours=48), 48)[0] is CheckResult.PASS


@pytest.mark.parametrize("observed_probe", [Probe.PLATFORM_ERRORS, Probe.ALERTS, Probe.ACCESS_LOG,
                                            Probe.PROJECT_PROBE])
def test_platform_and_probe_problems_and_inconclusive_checks_are_observed(observed_probe):
    sources = {"P-0001": Probe.API_FUZZ, "P-0002": observed_probe}
    passed = [Item("deploy-confirm:api-1", "deploy-confirm", CheckResult.PASS)]
    assert confirm.observed(sources, passed) == ["P-0002"]
    not_run = [Item("deploy-confirm:api-1", "deploy-confirm", CheckResult.UNVERIFIED)]
    assert confirm.observed(sources, not_run) == ["P-0001", "P-0002"]
    assert confirm.observed(sources, []) == ["P-0001", "P-0002"]


def test_the_decision_prefers_regressions_then_waits():
    assert confirm.decide([entry("replay", CheckResult.PASS), entry("observe", CheckResult.FAIL)]) == confirm.REGRESSED
    assert confirm.decide([entry("replay", CheckResult.PASS)]) == confirm.VERIFIED
    assert confirm.decide([entry("observe", CheckResult.UNVERIFIED)]) == confirm.WAITING
    assert confirm.decide([entry("replay", CheckResult.UNVERIFIED), entry("observe", CheckResult.PASS)]) == \
        confirm.VERIFIED
    assert confirm.decide([entry("replay", CheckResult.WEAK)]) == confirm.WAITING
