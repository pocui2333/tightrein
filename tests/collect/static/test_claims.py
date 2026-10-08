from datetime import UTC, date, datetime

from tightrein.collect.dedup.suppress import SuppressionRule
from tightrein.collect.static import claims
from tightrein.collect.static.claims import Claim, OpenProblem

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)


def make(file="src/a.py", line=2, rule="r1", severity="high", trigger="x 为空时", statement="会崩溃"):
    return Claim(file, line, rule, "incremental", severity, statement, trigger)


def worktree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("".join(f"line {number}\n" for number in range(30)), encoding="utf-8")
    return tmp_path


def screen(tmp_path, items, known=(), rules=()):
    return claims.screen(items, worktree=worktree(tmp_path), known=list(known), rules=list(rules), now=NOW,
                         nearby_lines=5, run="R-1")


def test_claims_without_a_location_or_a_trigger_are_dropped(tmp_path):
    result = screen(tmp_path, [make(), make(file="src/gone.py"), make(line=99), make(line=3, trigger="  ")])
    assert result.kept == [make()]
    assert result.dropped == {"noLocation": 2, "noTrigger": 1}


def test_duplicates_of_open_problems_and_repeats_are_dropped(tmp_path):
    known = [OpenProblem("src/a.py", 20), OpenProblem("src/b.py", 2), OpenProblem("src/a.py", None)]
    result = screen(tmp_path, [make(line=2), make(line=2), make(line=17), make(line=10)], known)
    assert [item.line for item in result.kept] == [2, 10]
    assert result.dropped == {"repeated": 1, "duplicate": 1}


def test_suppressed_claims_use_the_dedup_rules(tmp_path):
    rule = SuppressionRule(reason="误报", added_on=date(2026, 10, 1), expires_on=date(2026, 12, 1),
                           source="collect.static", message_pattern=__import__("re").compile("会崩溃"))
    result = screen(tmp_path, [make(), make(line=4, statement="会泄露")], rules=[rule])
    assert [item.statement for item in result.kept] == ["会泄露"] and result.dropped == {"suppressed": 1}


def test_top_keeps_the_most_severe():
    items = [make(line=1, severity="low"), make(line=2, severity="high"), make(line=3, severity="medium"),
             make(line=4, severity="high")]
    kept, cut = claims.top(items, 2)
    assert [item.line for item in kept] == [2, 4] and cut == 2


def test_queued_claims_go_first_and_low_and_overflow_wait():
    queued = [make(line=9, rule="old")]
    fresh = [make(line=1, severity="low"), make(line=2, severity="medium"), make(line=3, severity="high")]
    selection = claims.select(queued, fresh, 2)
    assert [(item.rule_or_pattern, item.line) for item in selection.verify] == [("old", 9), ("r1", 3)]
    assert [(item["claim"]["line"], item["reason"]) for item in selection.pending] == [(1, "low"), (2, "overLimit")]
    again, kept = claims.queued_claims(selection.pending)
    assert [item.line for item in again] == [2] and [item["reason"] for item in kept] == ["low"]


def test_claims_round_trip():
    assert Claim.from_json(make().to_json()) == make()
    assert make(severity="unknown").severity_rank == 3
