from datetime import UTC, datetime, timedelta

from tightrein.assess.cases import Case, classify, prepared
from tightrein.store.tables.occurrences import Occurrence

NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)
VERIFICATION = {"analysis": "读了代码", "report": {"title": "t"}, "assessment": {"worth": "fix"}, "verdict": "confirmed"}


def occurrence(number: int = 1, *, evidence: dict | None = None, **flags: object) -> Occurrence:
    return Occurrence(problem="P-0001", seen_at=NOW + timedelta(minutes=number), source="collect.static", id=number,
                      evidence={"signal": f"S-{number}", **flags, "evidence": dict(evidence or {})})


def test_static_problems_use_the_collected_verification_first():
    verified = occurrence(verified=True, deterministic=True, evidence={"verification": VERIFICATION})
    assert classify([verified], retriage=False) is Case.VERIFIED
    assert prepared(verified) == VERIFICATION
    # 用户请求的重新评估一律不复用，重新取证
    assert classify([verified], retriage=True) is Case.FULL
    # 旧格式(缺分析、报告或评估)的不复用
    old = occurrence(verified=True, deterministic=True, evidence={"verification": {"verdict": "confirmed"}})
    assert prepared(old) is None and classify([old], retriage=False) is Case.FULL


def test_reproducible_deterministic_p0_and_light_cases():
    assert classify([occurrence(1), occurrence(2, reproducible=True)], retriage=False) is Case.REPRODUCIBLE
    assert classify([occurrence(deterministic=True)], retriage=False) is Case.FULL
    assert classify([occurrence(severityHint="P0")], retriage=False) is Case.FULL
    assert classify([occurrence()], retriage=False) is Case.LIGHT
    assert classify([occurrence()], retriage=True) is Case.FULL
