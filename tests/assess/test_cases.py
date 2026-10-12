from datetime import UTC, datetime, timedelta

from tightrein.assess.cases import Case, classify, complete, prepared
from tightrein.store.tables.occurrences import Occurrence

NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)
COLLECTED = {"analysis": "读了代码", "verdict": "confirmed", "facts": [{"location": "a.py:1", "observation": "x"}],
             "counterEvidence": [{"check": "c", "entry": "a.py:1", "result": "r"}]}
VERIFICATION = {**COLLECTED, "report": {"title": "t"}, "assessment": {"worth": "fix"}}


def occurrence(number: int = 1, *, evidence: dict | None = None, **flags: object) -> Occurrence:
    return Occurrence(problem="P-0001", seen_at=NOW + timedelta(minutes=number), source="collect.static", id=number,
                      evidence={"signal": f"S-{number}", **flags, "evidence": dict(evidence or {})})


def test_static_problems_use_the_collected_verification_first():
    verified = occurrence(verified=True, deterministic=True, evidence={"verification": VERIFICATION})
    assert classify([verified], retriage=False) is Case.VERIFIED
    assert prepared(verified) == VERIFICATION
    # 用户请求的重新评估一律不复用，重新取证
    assert classify([verified], retriage=True) is Case.FULL
    assert complete(prepared(verified))
    # 静态巡检的取证只有结论、没有报告与评估：仍算已取证(交给确认补报告与评估)，不从零调查
    collected = occurrence(verified=True, deterministic=True, evidence={"verification": COLLECTED})
    assert classify([collected], retriage=False) is Case.VERIFIED and not complete(prepared(collected))
    # 缺代码事实或反证的、判不成立或证据不足的不复用
    for value in ({"verdict": "confirmed"}, {**COLLECTED, "verdict": "insufficient"}, {**COLLECTED, "facts": []}):
        old = occurrence(verified=True, deterministic=True, evidence={"verification": value})
        assert prepared(old) is None and classify([old], retriage=False) is Case.FULL


def test_reproducible_deterministic_p0_and_light_cases():
    assert classify([occurrence(1), occurrence(2, reproducible=True)], retriage=False) is Case.REPRODUCIBLE
    assert classify([occurrence(deterministic=True)], retriage=False) is Case.FULL
    assert classify([occurrence(severityHint="P0")], retriage=False) is Case.FULL
    assert classify([occurrence()], retriage=False) is Case.LIGHT
    assert classify([occurrence()], retriage=True) is Case.FULL
