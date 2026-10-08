from datetime import UTC, datetime

from tightrein.agents.result import CallResult, CallStatus
from tightrein.collect.common.signals import SignalFactory, SignalLimits
from tightrein.collect.static import mapping
from tightrein.collect.static.claims import Claim, ToolFinding
from tightrein.collect.static.verify import Verification
from tightrein.protocol.git import GitError
from tightrein.protocol.naming import FixedClock
from tightrein.protocol.raw import RawDir
from tightrein.protocol.security import Redactor

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)


class BlameGit:
    def __init__(self, fail=False):
        self.fail = fail
        self.asked = []

    def blame(self, path, start, end, rev):
        self.asked.append((path, start, rev))
        if self.fail:
            raise GitError("blame 失败")
        return [type("Line", (), {"commit": "abc123", "author": "Cui Ty", "time": NOW})()]


def factory(tmp_path):
    return SignalFactory(run="R-1", source="collect.static", clock=FixedClock(NOW), redactor=Redactor(),
                         raw=RawDir(tmp_path), limits=SignalLimits(1000, 65536, 500))


def verified(make_verdict, value="confirmed", rule="unbounded-page", **verdict):
    claim = Claim("src/orders.py", 3, rule, "incremental", "high", "page 没有上限", "page 很大时")
    return Verification(claim, CallResult(CallStatus.OK, "claude", "opus", output=make_verdict(value, **verdict)),
                        NOW)


def test_only_confirmed_and_conditional_claims_become_verified_signals(tmp_path, make_verdict):
    git = BlameGit()
    items = [verified(make_verdict), verified(make_verdict, "conditional", rule="r2", line=4, symbol=None),
             verified(make_verdict, "refuted", rule="r3"), verified(make_verdict, "insufficient", rule="r4")]
    signals = mapping.to_signals(items, [], factory(tmp_path), head="h1", git=git)
    assert [(item.location, item.symbol) for item in signals] == [("src/orders.py:3", "list_orders"),
                                                                  ("src/orders.py:4", None)]
    first, second = signals
    assert first.verified and first.deterministic and first.check_type == "static" and first.commit == "h1"
    assert first.evidence["rule"] == "unbounded-page" and first.evidence["verdict"] == {"value": "confirmed",
                                                                                         "condition": None}
    assert second.evidence["verdict"] == {"value": "conditional", "condition": "page=-1"}
    assert first.evidence["introducedBy"] == {"commit": "abc123", "author": "Cui Ty", "date": "2026-10-05T03:00:00Z"}
    assert first.evidence["callChain"] == ["src/orders.py:1"] and first.evidence["verification"]["verdict"] == "confirmed"
    assert git.asked[0] == ("src/orders.py", 3, "h1")


def test_dependency_vulnerabilities_and_failed_blame(tmp_path, make_verdict):
    finding = ToolFinding("deps", "vulnerability", "unbounded-page", "src/orders.py", None, None, "漏洞", "high",
                          {"name": "requests", "version": "2.0"})
    signals = mapping.to_signals([verified(make_verdict)], [finding], factory(tmp_path), head="h1",
                                 git=BlameGit(fail=True))
    assert signals[0].location == "src/orders.py:requests" and signals[0].evidence["introducedBy"] is None
    assert signals[0].evidence["toolFinding"]["package"]["name"] == "requests"


def test_without_root_causes_the_claim_location_is_used(tmp_path, make_verdict):
    item = verified(make_verdict)
    item.result.output["rootCauses"] = []
    assert mapping.location(item, None) == ("src/orders.py:3", None, 3)


def test_rule_library_hits_become_signals_without_review(tmp_path):
    hit = ToolFinding("semgrep", "lint", "PAT-0003-no-limit", "src/orders.py", 3, 5, "分页没有上限", "high")
    signals = mapping.rule_signals([hit], factory(tmp_path), head="h1", now=NOW)
    assert [(item.location, item.evidence["rule"], item.verified) for item in signals] == [
        ("src/orders.py:3", "rule:PAT-0003-no-limit", False)]
