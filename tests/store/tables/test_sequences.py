import sqlite3
import threading
from pathlib import Path

import pytest

from tightrein.store.db import connect, transaction
from tightrein.store.tables import sequences


def test_values_start_at_one_and_increase(conn: sqlite3.Connection) -> None:
    assert sequences.current(conn, "problem") == 0
    assert [sequences.next_value(conn, "problem") for _ in range(3)] == [1, 2, 3]
    assert sequences.current(conn, "problem") == 3
    assert sequences.next_value(conn, "issue") == 1


def test_ensure_at_least_only_moves_forward(conn: sqlite3.Connection) -> None:
    sequences.ensure_at_least(conn, "issue", 7)
    assert sequences.next_value(conn, "issue") == 8
    sequences.ensure_at_least(conn, "issue", 3)
    assert sequences.current(conn, "issue") == 8


def test_a_rolled_back_allocation_is_not_used(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError), transaction(conn):
        assert sequences.next_value(conn, "problem") == 1
        raise RuntimeError("回滚")
    assert sequences.next_value(conn, "problem") == 1


def test_concurrent_allocations_never_repeat(conn: sqlite3.Connection, db_path: Path) -> None:
    results: list[int] = []
    errors: list[sqlite3.Error] = []
    guard = threading.Lock()

    def allocate() -> None:
        connection = connect(db_path)
        try:
            values = [sequences.next_value(connection, "problem") for _ in range(50)]
        except sqlite3.Error as error:
            errors.append(error)
            return
        finally:
            connection.close()
        with guard:
            results.extend(values)

    threads = [threading.Thread(target=allocate) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(results) == list(range(1, 401))
