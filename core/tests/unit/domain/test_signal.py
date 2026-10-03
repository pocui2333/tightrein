from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tightrein.domain.enums import Probe, SignalAggregateState, Source
from tightrein.domain.signal import Signal


def make_signal(**overrides):
    base = dict(
        id="S-" + "0" * 26,
        run_id="R-20260929-021503-collect-api-fuzz",
        source=Source.SYNTHETIC,
        probe=Probe.API_FUZZ,
        check="not_a_server_error",
        environment="staging",
        occurred_at=datetime(2026, 9, 29, 2, 15, 3, tzinfo=timezone.utc),
        release="d6f37025",
        location="POST /api/Order/Query",
        message="服务端返回 500",
        context={"role": "Company", "response": {"status": 500}},
        actor={"id": "test-company", "role": "Company"},
    )
    base.update(overrides)
    return Signal(**base)


def test_defaults():
    signal = make_signal()
    assert signal.normalized_message is None
    assert signal.fingerprint is None
    assert signal.suppressed is False
    assert signal.aggregate_state is SignalAggregateState.PENDING


def test_occurred_at_must_be_timezone_aware():
    with pytest.raises(ValueError):
        make_signal(occurred_at=datetime(2026, 9, 29, 2, 15, 3))


def test_context_value_helper():
    signal = make_signal()
    assert signal.ctx("response", "status") == 500
    assert signal.ctx("missing", "x") is None


def test_document_round_trip_matches_the_schema():
    from tightrein.contracts import validate as contracts

    signal = replace(make_signal(), normalized_message="服务端返回 <num>", fingerprint="0123456789abcdef",
                     aggregate_state=SignalAggregateState.DONE)
    document = signal.to_dict()
    assert contracts.validate("data/signal.schema.json", document) == []
    assert Signal.from_dict(document) == signal


def test_document_without_aggregate_fields_is_unaggregated():
    document = make_signal().to_dict()
    for key in ("normalizedMessage", "fingerprint", "suppressed", "aggregateState"):
        document.pop(key)
    assert Signal.from_dict(document) == make_signal()
