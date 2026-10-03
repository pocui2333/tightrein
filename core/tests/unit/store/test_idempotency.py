from datetime import timedelta

import pytest

from tightrein.store import idempotency
from tightrein.store.idempotency import DONE, IN_PROGRESS, IdempotencyRecord, InProgress

KEY = "push:fix/0007-order-owner-check"


def test_begin_writes_an_in_progress_key_once(conn, clock):
    first = idempotency.begin(conn, KEY, clock)
    clock.advance(timedelta(minutes=1))
    second = idempotency.begin(conn, KEY, clock)
    assert first.created and not second.created
    assert first.record == second.record == IdempotencyRecord(KEY, IN_PROGRESS, first.record.created_at)


def test_complete_records_the_result(conn, clock):
    idempotency.begin(conn, KEY, clock)
    clock.advance(timedelta(seconds=30))
    done = idempotency.complete(conn, KEY, {"pr": 42}, clock)
    assert idempotency.get(conn, KEY) == done
    assert (done.status, done.result, done.completed_at) == (DONE, {"pr": 42}, clock.now())
    with pytest.raises(LookupError):
        idempotency.complete(conn, "missing", {}, clock)


def test_run_once_executes_and_then_skips(conn, clock):
    calls = []

    def action():
        calls.append(1)
        return {"commit": "abc1234"}

    first = idempotency.run_once(conn, KEY, action, clock)
    second = idempotency.run_once(conn, KEY, action, clock)
    assert (first.result, first.skipped) == ({"commit": "abc1234"}, False)
    assert (second.result, second.skipped) == ({"commit": "abc1234"}, True)
    assert calls == [1]


def test_a_failed_action_can_be_retried(conn, clock):
    def failing():
        raise ConnectionError("推送被拒绝")

    with pytest.raises(ConnectionError):
        idempotency.run_once(conn, KEY, failing, clock)
    assert idempotency.get(conn, KEY) is None
    assert idempotency.run_once(conn, KEY, lambda: {"ok": True}, clock).skipped is False


def test_an_interrupted_key_needs_reconciliation(conn, clock):
    idempotency.begin(conn, KEY, clock)
    with pytest.raises(InProgress, match="核对实际状态") as error:
        idempotency.run_once(conn, KEY, lambda: {}, clock)
    assert error.value.record.status == IN_PROGRESS
    assert idempotency.abandon(conn, KEY) is True
    assert idempotency.run_once(conn, KEY, lambda: {"ok": True}, clock).result == {"ok": True}
    assert idempotency.abandon(conn, KEY) is False
    assert idempotency.get(conn, KEY).status == DONE


def test_status_values_are_checked():
    with pytest.raises(ValueError):
        IdempotencyRecord(KEY, "failed", None)


def test_find_lists_keys_by_prefix(conn, clock):
    idempotency.run_once(conn, "variant:0007", lambda: {"issueId": "0007"}, clock)
    idempotency.begin(conn, "variant:0008", clock)
    idempotency.begin(conn, "variant-scan:0007", clock)
    assert [record.key for record in idempotency.find(conn, "variant:")] == ["variant:0007", "variant:0008"]
