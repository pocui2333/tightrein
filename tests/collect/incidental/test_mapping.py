from datetime import UTC, datetime

from tightrein.collect.common.signals import SignalFactory, SignalLimits
from tightrein.collect.incidental import mapping
from tightrein.collect.incidental.mapping import Finding
from tightrein.protocol.naming import FixedClock
from tightrein.protocol.raw import RawDir
from tightrein.protocol.security import Redactor

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)


def finding(**changes):
    values = {"file": "src/A.cs", "line": 10, "symbol": "A.Run", "category": "defect", "confidence": "suspected",
              "evidence": "没有判空", "text": "A.Run 缺少空值判断", "source_path": "data/problems/P-0001/x.json",
              "point": "assess.triage", "subject": "P-0001", "run": "R-20261004T020000Z-assess",
              "occurred_at": datetime(2026, 10, 4, 2, 30, tzinfo=UTC), "commit": "c1"}
    return Finding(**{**values, **changes})


def test_findings_become_signals_and_other_categories_are_dropped(tmp_path):
    factory = SignalFactory(run="R-20261005T030000Z-collect", source="collect.incidental", clock=FixedClock(NOW),
                            redactor=Redactor(), raw=RawDir(tmp_path), limits=SignalLimits(1000, 16384, 2000))
    signals, dropped = mapping.to_signals([finding(), finding(category="style"), finding(line=None, symbol=None,
                                                                                         category="security")], factory)
    assert dropped == 1
    first, second = signals
    assert (first.check_type, first.location, first.symbol, first.message, first.commit) == (
        "incidental:defect", "src/A.cs:10", "A.Run", "A.Run 缺少空值判断", "c1")
    assert first.group_key == "incidental:src/A.cs:A.Run:defect"
    assert first.evidence["confidence"] == "suspected" and first.evidence["sourceSubject"] == "P-0001"
    assert (second.location, second.group_key) == ("src/A.cs", "incidental:src/A.cs::security")


def test_the_fingerprint_ignores_the_line_and_keeps_the_category():
    assert mapping.fingerprint(finding(line=10)) == mapping.fingerprint(finding(line=99))
    assert mapping.fingerprint(finding()) != mapping.fingerprint(finding(category="performance"))
