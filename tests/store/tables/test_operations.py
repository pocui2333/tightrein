import json
import sqlite3

import pytest

from tightrein.protocol.naming import FixedClock
from tightrein.store.tables import operations
from tightrein.store.tables.operations import DONE, IN_PROGRESS, InProgress, Operation

KEY = "0018:release.pr:create_pr:3f2a"


def test_run_once_executes_and_then_skips(conn: sqlite3.Connection, clock: FixedClock) -> None:
    calls: list[int] = []

    def action() -> dict[str, int]:
        calls.append(1)
        return {"pr": 42}

    assert operations.run_once(conn, KEY, action, clock, subject="0018", point="release.pr") == {"pr": 42}
    assert operations.run_once(conn, KEY, action, clock) == {"pr": 42}
    assert calls == [1]
    assert operations.get(conn, KEY) == Operation(KEY, DONE, '{"pr": 42}', "0018", "release.pr")


def test_custom_encoding_round_trips(conn: sqlite3.Connection, clock: FixedClock) -> None:
    encode = lambda value: json.dumps(sorted(value))
    decode = lambda text: set(json.loads(text))
    first = operations.run_once(conn, KEY, lambda: {"b", "a"}, clock, encode=encode, decode=decode)
    again = operations.run_once(conn, KEY, lambda: set(), clock, encode=encode, decode=decode)
    assert first == again == {"a", "b"}


def test_a_failed_action_can_be_retried(conn: sqlite3.Connection, clock: FixedClock) -> None:
    def failing() -> None:
        raise ConnectionError("推送被拒绝")

    with pytest.raises(ConnectionError):
        operations.run_once(conn, KEY, failing, clock)
    assert operations.get(conn, KEY) is None
    assert operations.run_once(conn, KEY, lambda: {"ok": True}, clock) == {"ok": True}


def test_an_interrupted_key_needs_reconciliation(conn: sqlite3.Connection, clock: FixedClock) -> None:
    def interrupted() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        operations.run_once(conn, KEY, interrupted, clock)
    assert operations.get(conn, KEY).status == IN_PROGRESS  # type: ignore[union-attr]
    with pytest.raises(InProgress, match="核对实际状态") as error:
        operations.run_once(conn, KEY, dict, clock)
    assert error.value.key == KEY
    operations.abandon(conn, KEY)
    assert operations.run_once(conn, KEY, lambda: {"ok": True}, clock) == {"ok": True}
    operations.abandon(conn, KEY)
    assert operations.get(conn, KEY).status == DONE  # type: ignore[union-attr]


def test_complete_after_reconciliation(conn: sqlite3.Connection, clock: FixedClock) -> None:
    operations.complete(conn, KEY, {"pr": 7}, clock)
    assert operations.run_once(conn, KEY, lambda: {"pr": 8}, clock) == {"pr": 7}


def test_status_values_are_checked() -> None:
    with pytest.raises(ValueError):
        Operation(KEY, "failed")
