import json
import re
from datetime import UTC, datetime, timedelta, timezone

import pytest

from tightrein.collect.common.signals import (
    DEPLOYMENTS_KEY,
    Deployment,
    SignalFactory,
    SignalLimits,
    deployments,
    latest_release,
    release_at,
    releases,
    serialized_size,
    signal_id,
)
from tightrein.protocol.naming import FixedClock
from tightrein.protocol.raw import RawDir
from tightrein.protocol.security import Redactor
from tightrein.store.tables import state

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
LIMITS = SignalLimits(message_chars=1000, evidence_bytes=16 * 1024, excerpt_chars=500)


class CountingRandom:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self, size: int) -> bytes:
        self.count += 1
        return self.count.to_bytes(size, "big")


def factory(tmp_path, *secrets):
    redactor = Redactor()
    for secret in secrets:
        redactor.register(secret)
    return SignalFactory(run="R-20261005T030000Z-collect", source="collect.api_fuzz", clock=FixedClock(NOW),
                         redactor=redactor, raw=RawDir(tmp_path / "raw"), limits=LIMITS, randomness=CountingRandom())


def create(make, **changes):
    values = {"check_type": "server_error", "location": "GET /a", "message": "m", "occurred_at": NOW,
              "commit": None, "evidence": {}}
    return make.create(**{**values, **changes})


def test_signal_fields(tmp_path):
    occurred = datetime(2026, 10, 5, 11, 15, 3, 999000, tzinfo=timezone(timedelta(hours=8)))
    signal = create(factory(tmp_path), location="POST /api/Order/Query", message="服务端返回 500",
                    occurred_at=occurred, commit="d6f3", evidence={"role": "Company"}, severity_hint="P1",
                    deterministic=True)
    assert re.fullmatch(r"S-[0-9A-HJKMNP-TV-Z]{26}", signal.id)
    assert (signal.run, signal.source, signal.commit) == ("R-20261005T030000Z-collect", "collect.api_fuzz", "d6f3")
    assert signal.occurred_at == "2026-10-05T03:15:03Z"
    assert signal.deterministic and not signal.verified and signal.severity_hint == "P1"
    assert signal.to_json()["checkType"] == "server_error" and "groupKey" in signal.to_json()


def test_signal_ids_differ_and_message_is_redacted_and_truncated(tmp_path):
    make = factory(tmp_path, "hunter2")
    first = create(make, message="password hunter2 " + "y" * 2000)
    second = create(make)
    assert first.id != second.id
    assert len(first.message) == 1000 and "hunter2" not in first.message


def test_ulid_ids_sort_by_time():
    early = signal_id(1_000, bytes(10))
    late = signal_id(2_000, bytes(10))
    assert early < late
    with pytest.raises(ValueError):
        signal_id(1_000, bytes(5))


def test_evidence_is_redacted_but_reproduce_is_kept(tmp_path):
    signal = create(factory(tmp_path), evidence={
        "reproduce": "curl -H 'Authorization: Bearer <TOKEN>'", "pageUrl": "/a?token=abc", "request": {"cookie": "x"}})
    assert signal.evidence["reproduce"] == "curl -H 'Authorization: Bearer <TOKEN>'"
    assert "abc" not in signal.evidence["pageUrl"]
    assert signal.evidence["request"]["cookie"].startswith("[REDACTED")


def test_large_evidence_items_move_to_raw(tmp_path):
    make = factory(tmp_path)
    big = {"rows": ["x" * 100] * 300}
    signal = create(make, evidence={"role": "Company", "body": big, "small": 1})
    assert serialized_size(signal.evidence) <= LIMITS.evidence_bytes
    reference = signal.evidence["bodyRef"]
    assert reference == f"refs/{signal.id}-body.json"
    assert json.loads((tmp_path / "raw" / reference).read_text(encoding="utf-8")) == big
    assert signal.evidence["role"] == "Company" and "body" not in signal.evidence


def test_evidence_still_too_large_with_only_references_is_an_error(tmp_path):
    make = factory(tmp_path)
    references = {f"part{number}Ref": "refs/" + "x" * 1000 for number in range(20)}
    with pytest.raises(ValueError, match="只剩引用仍超过上限"):
        make.fit("S-0001", {"body": "y" * 100, **references})
    assert (tmp_path / "raw" / "refs" / "S-0001-body.json").is_file()  # 能移的先移走了


def test_empty_location_and_unknown_severity_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="location"):
        create(factory(tmp_path), location=" ")
    with pytest.raises(ValueError, match="P0"):
        create(factory(tmp_path), severity_hint="critical")
    assert create(factory(tmp_path), location=None).location is None


def test_excerpts_are_redacted_then_truncated(tmp_path):
    excerpt = factory(tmp_path, "s3cret-value").excerpt("password=s3cret-value " + "x" * 600)
    assert len(excerpt) == 500 and "s3cret" not in excerpt


def test_releases_follow_successful_deployments(source_conn, source_clock):
    found = [Deployment("b", "succeeded", datetime(2026, 10, 2, tzinfo=UTC), datetime(2026, 10, 2, 1, tzinfo=UTC)),
             Deployment("a", "succeeded", datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 1, 1, tzinfo=UTC)),
             Deployment("c", "failed", datetime(2026, 10, 3, tzinfo=UTC), datetime(2026, 10, 3, tzinfo=UTC)),
             Deployment("e", "succeeded", None, datetime(2026, 10, 4, tzinfo=UTC)),
             Deployment("d", "succeeded", None, datetime(2026, 10, 4, tzinfo=UTC))]
    assert release_at(found, datetime(2026, 9, 30, tzinfo=UTC)) is None
    assert release_at(found, datetime(2026, 10, 3, 12, tzinfo=UTC)) == "b"
    assert release_at(found, datetime(2026, 10, 4, tzinfo=UTC)) == "e"
    assert latest_release(found) == "e"
    state.put(source_conn, DEPLOYMENTS_KEY, [
        {"commit": "a", "status": "succeeded", "deployedAt": "2026-10-01T00:00:00Z",
         "detectedAt": "2026-10-01T01:00:00Z"}], source_clock)
    assert deployments(source_conn)[0].commit == "a"
    assert releases(source_conn)(NOW) == "a"
