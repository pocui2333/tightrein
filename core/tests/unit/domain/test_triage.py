import pytest

from tightrein.domain.enums import (
    Disposition,
    ImpactKind,
    Severity,
    SizeTier,
    Treatment,
    Verdict,
    WorthRecommendation,
)
from tightrein.domain.triage import (
    SEVERITY_BY_IMPACT,
    TreatmentFacts,
    TreatmentRule,
    TriageResult,
    severity,
    treatment,
    urgency_key,
)


@pytest.mark.parametrize("impact,expected", [
    (ImpactKind.AUTHORIZATION, Severity.P0),
    (ImpactKind.DATA_OWNERSHIP, Severity.P0),
    (ImpactKind.DATA_CORRECTNESS, Severity.P0),
    (ImpactKind.CREDENTIAL_LEAK, Severity.P0),
    (ImpactKind.CORE_FLOW_BROKEN, Severity.P1),
    (ImpactKind.NON_CORE_ERROR, Severity.P2),
    (ImpactKind.CONTRACT_MISMATCH, Severity.P2),
    (ImpactKind.EXPERIENCE, Severity.P3),
    (ImpactKind.SLOW_RESPONSE, Severity.P3),
    (ImpactKind.DEPENDENCY_VULNERABILITY, Severity.P3),
])
def test_severity_by_impact(impact, expected):
    assert severity(impact) is expected


def test_every_impact_is_mapped():
    assert set(SEVERITY_BY_IMPACT) == set(ImpactKind)


# 与 defaults.yaml 的 triage.treatment.rules 相同
DEFAULT_TREE = [TreatmentRule.from_config(item) for item in (
    {"worth": ["wont"], "treatment": "wont-fix"},
    {"severities": ["P0"], "treatment": "immediate"},
    {"severities": ["P1"], "verdicts": ["confirmed"], "treatment": "immediate"},
    {"worth": ["defer"], "treatment": "observe"},
    {"severities": ["P1", "P2"], "treatment": "scheduled"},
    {"severities": ["P3"], "tiers": ["micro", "small"], "worth": ["fix"], "treatment": "scheduled"},
    {"treatment": "observe"},
)]
CONFIRMED, CONDITIONAL = Verdict.CONFIRMED, Verdict.CONDITIONAL
FIX, DEFER, WONT = WorthRecommendation.FIX, WorthRecommendation.DEFER, WorthRecommendation.WONT


@pytest.mark.parametrize("facts,expected", [
    (TreatmentFacts(CONFIRMED, Severity.P0, SizeTier.LARGE, WONT), Treatment.WONT_FIX),
    (TreatmentFacts(CONDITIONAL, Severity.P0, SizeTier.LARGE, DEFER), Treatment.IMMEDIATE),
    (TreatmentFacts(CONFIRMED, Severity.P1, SizeTier.MEDIUM, FIX), Treatment.IMMEDIATE),
    (TreatmentFacts(CONDITIONAL, Severity.P1, SizeTier.MEDIUM, FIX), Treatment.SCHEDULED),
    (TreatmentFacts(CONFIRMED, Severity.P2, SizeTier.SMALL, DEFER), Treatment.OBSERVE),
    (TreatmentFacts(CONFIRMED, Severity.P2, SizeTier.SMALL, FIX), Treatment.SCHEDULED),
    (TreatmentFacts(CONFIRMED, Severity.P3, SizeTier.MICRO, FIX), Treatment.SCHEDULED),
    (TreatmentFacts(CONFIRMED, Severity.P3, SizeTier.MEDIUM, FIX), Treatment.OBSERVE),
    (TreatmentFacts(CONFIRMED, None, None, None), Treatment.OBSERVE),
])
def test_default_treatment_tree(facts, expected):
    assert treatment(facts, DEFAULT_TREE) is expected


def test_tree_without_fallback_is_an_error():
    with pytest.raises(ValueError):
        treatment(TreatmentFacts(CONFIRMED, Severity.P3, None, None), DEFAULT_TREE[:2])


def test_urgency_key_orders_treatment_then_severity():
    keys = [urgency_key(Treatment.SCHEDULED, Severity.P0), urgency_key(Treatment.IMMEDIATE, Severity.P2),
            urgency_key(None, Severity.P0), urgency_key(Treatment.IMMEDIATE, Severity.P1)]
    assert sorted(keys) == [urgency_key(Treatment.IMMEDIATE, Severity.P1), urgency_key(Treatment.IMMEDIATE, Severity.P2),
                            urgency_key(Treatment.SCHEDULED, Severity.P0), urgency_key(None, Severity.P0)]


def test_triage_result_validation():
    base = dict(problem_id="P-0001", verdict=Verdict.REFUTED, disposition=Disposition.FALSE_POSITIVE,
                reason="上游已校验", triage_commit="c1")
    assert TriageResult(attempt=1, **base).treatment is None
    with pytest.raises(ValueError):
        TriageResult(attempt=0, **base)
    with pytest.raises(ValueError):
        TriageResult(attempt=1, treatment=Treatment.OBSERVE, **base)
