import re
from datetime import UTC, date, datetime

import pytest

from tightrein.collect.dedup import suppress
from tightrein.collect.dedup.suppress import SuppressionInvalid, SuppressionRule, add, apply, load, match

TODAY = date(2026, 10, 7)
NOON = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def by_message(source="collect.platform_errors", pattern="boom", expires=TODAY, reason="误报"):
    return SuppressionRule(reason=reason, added_on=TODAY, expires_on=expires, source=source,
                           message_pattern=re.compile(pattern))


def test_match_by_fingerprint_or_by_source_and_message(make_signal):
    signal = make_signal(message="boom at x")
    assert match(signal, "f1", [SuppressionRule.for_fingerprint("f1", "误报", TODAY, 30)], NOON) is not None
    assert match(signal, "f2", [SuppressionRule.for_fingerprint("f1", "误报", TODAY, 30)], NOON) is None
    assert match(signal, "f2", [by_message()], NOON) is not None
    assert match(signal, "f2", [by_message(source="collect.alerts")], NOON) is None


def test_expired_rule_is_ignored_but_expiry_day_still_counts(make_signal):
    signal = make_signal(message="boom")
    rule = by_message(expires=TODAY)
    assert match(signal, "f", [rule], NOON) is rule
    assert match(signal, "f", [rule], datetime(2026, 10, 8, 12, 0, tzinfo=UTC)) is None


def test_first_active_rule_wins(make_signal):
    signal = make_signal(message="boom")
    first, second = by_message(reason="一"), by_message(reason="二")
    assert match(signal, "f", [first, second], NOON) is first


def test_regression_signal_only_matches_by_message(make_signal):
    signal = make_signal(message="boom", evidence={"targetFingerprints": ["f"]})
    assert match(signal, None, [SuppressionRule.for_fingerprint("f", "误报", TODAY, 30)], NOON) is None
    assert match(signal, None, [by_message()], NOON) is not None


def test_invalid_match_condition():
    with pytest.raises(SuppressionInvalid):
        SuppressionRule(reason="r", added_on=TODAY, expires_on=TODAY, fingerprint="f", source="collect.alerts",
                        message_pattern=re.compile("x"))
    with pytest.raises(SuppressionInvalid):
        SuppressionRule(reason="r", added_on=TODAY, expires_on=TODAY)
    with pytest.raises(SuppressionInvalid) as caught:
        suppress.rules_from([{"match": {"fingerprint": "f"}}, {"reason": "x"}], "where")
    assert "where[0]" in str(caught.value) and "where[1]" in str(caught.value)


def test_for_fingerprint_expires_after_the_given_days():
    rule = SuppressionRule.for_fingerprint("f", "误报", TODAY, 30)
    assert rule.expires_on == date(2026, 11, 6) and rule.fingerprint == "f"


def test_project_rules_come_before_stored_rules_and_new_rules_replace_old(conn, clock, make_signal):
    configured = [{"match": {"source": "collect.platform_errors", "messagePattern": "boom"}, "reason": "项目",
                   "addedOn": "2026-10-01", "expiresOn": "2026-12-31"}]
    add(conn, SuppressionRule.for_fingerprint("f", "旧", TODAY, 1), clock)
    add(conn, SuppressionRule.for_fingerprint("f", "新", TODAY, 30), clock)
    rules = load(conn, configured)
    assert [rule.reason for rule in rules] == ["项目", "新"]
    kept, dropped = apply([make_signal(message="boom"), make_signal(message="other")], rules, NOON,
                          lambda signal: "x")
    assert dropped == 1 and [signal.message for signal in kept] == ["other"]
