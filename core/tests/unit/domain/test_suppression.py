import re
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from tightrein.domain.enums import Probe, Source
from tightrein.domain.fingerprint import fingerprint
from tightrein.domain.signal import Signal
from tightrein.domain.suppression import SuppressionRule, match

NOW = datetime(2026, 9, 29, 2, 15, tzinfo=timezone.utc)
TODAY = NOW.date()

SIGNAL = Signal(
    id="S-" + "0" * 26, run_id="R-20260929-021500-collect-api-fuzz", source=Source.SYNTHETIC,
    probe=Probe.API_FUZZ, check="not_a_server_error", environment="staging", occurred_at=NOW, release="c1",
    location="POST /api/Order/Query", message="服务端返回 500", context={"response": {"status": 500}},
)
FINGERPRINT = fingerprint(SIGNAL, 1)


def rule(**kwargs):
    fields = dict(reason="误报", added_on=date(2026, 9, 1), expires_on=date(2026, 9, 30))
    fields.update(kwargs)
    return SuppressionRule(**fields)


def test_match_by_fingerprint_computed_when_missing():
    by_fingerprint = rule(fingerprint=FINGERPRINT)
    assert match(SIGNAL, [by_fingerprint], NOW) is by_fingerprint
    assert match(replace(SIGNAL, fingerprint=FINGERPRINT), [by_fingerprint], NOW) is by_fingerprint


def test_match_by_probe_and_message_pattern():
    by_message = rule(probe=Probe.API_FUZZ, message_pattern=r"返回 5\d\d")
    assert match(SIGNAL, [by_message], NOW) is by_message
    assert match(SIGNAL, [rule(probe=Probe.ALERTS, message_pattern="返回")], NOW) is None
    assert match(SIGNAL, [rule(probe=Probe.API_FUZZ, message_pattern="超时")], NOW) is None


def test_expired_rule_is_ignored_but_expiry_day_still_counts():
    assert match(SIGNAL, [rule(fingerprint=FINGERPRINT, expires_on=TODAY)], NOW) is not None
    assert match(SIGNAL, [rule(fingerprint=FINGERPRINT, expires_on=date(2026, 9, 28))], NOW) is None


def test_first_active_rule_wins():
    expired = rule(fingerprint=FINGERPRINT, expires_on=date(2026, 9, 28))
    first = rule(probe=Probe.API_FUZZ, message_pattern="500")
    second = rule(fingerprint=FINGERPRINT)
    assert match(SIGNAL, [expired, first, second], NOW) is first


def test_regression_signal_only_matches_by_message():
    regression = replace(SIGNAL, check="regression")
    assert match(regression, [rule(fingerprint=FINGERPRINT)], NOW) is None
    assert match(regression, [rule(probe=Probe.API_FUZZ, message_pattern="500")], NOW) is not None


@pytest.mark.parametrize("kwargs", [
    {},
    {"fingerprint": "f", "probe": Probe.API_FUZZ},
    {"probe": Probe.API_FUZZ},
    {"message_pattern": "x"},
])
def test_invalid_match_condition(kwargs):
    with pytest.raises(ValueError):
        rule(**kwargs)


def test_invalid_dates_and_pattern():
    with pytest.raises(ValueError):
        rule(fingerprint="f", expires_on=date(2026, 8, 1))
    with pytest.raises(re.error):
        rule(probe=Probe.API_FUZZ, message_pattern="(")


def test_for_fingerprint_uses_given_days():
    created = SuppressionRule.for_fingerprint("abc", "判为误报", TODAY, days=30)
    assert created.added_on == TODAY
    assert created.expires_on == date(2026, 10, 29)


def test_round_trip():
    for item in (rule(fingerprint="abc"), rule(probe=Probe.ALERTS, message_pattern="ResizeObserver")):
        assert SuppressionRule.from_dict(item.to_dict()) == item
    assert rule(fingerprint="abc").to_dict() == {
        "match": {"fingerprint": "abc"}, "reason": "误报", "addedOn": "2026-09-01", "expiresOn": "2026-09-30",
    }
