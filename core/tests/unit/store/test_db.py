import sqlite3

import pytest

from tightrein.store.db import connect, transaction


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "data" / "tightrein.db")
    connection.execute("CREATE TABLE items (name TEXT PRIMARY KEY, value INTEGER)")
    yield connection
    connection.close()


def names(connection):
    return [row["name"] for row in connection.execute("SELECT name FROM items ORDER BY name")]


def test_connect_creates_the_directory_and_enables_wal(tmp_path):
    connection = connect(tmp_path / "a" / "b" / "tightrein.db")
    assert (tmp_path / "a" / "b" / "tightrein.db").exists()
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    connection.close()


def test_connect_sets_busy_timeout_and_foreign_keys(tmp_path):
    connection = connect(tmp_path / "tightrein.db")
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    connection.close()
    other = connect(tmp_path / "tightrein.db", busy_timeout_ms=250)
    assert other.execute("PRAGMA busy_timeout").fetchone()[0] == 250
    other.close()


def test_rows_are_accessed_by_column_name(conn):
    conn.execute("INSERT INTO items VALUES ('a', 1)")
    row = conn.execute("SELECT name, value FROM items").fetchone()
    assert (row["name"], row["value"]) == ("a", 1)


def test_statements_outside_a_transaction_commit_immediately(conn, tmp_path):
    conn.execute("INSERT INTO items VALUES ('a', 1)")
    other = connect(tmp_path / "data" / "tightrein.db")
    assert names(other) == ["a"]
    other.close()


def test_transaction_commits_and_yields_the_connection(conn):
    with transaction(conn) as inner:
        assert inner is conn
        assert conn.in_transaction
        conn.execute("INSERT INTO items VALUES ('a', 1)")
    assert not conn.in_transaction
    assert names(conn) == ["a"]


def test_transaction_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError):
        with transaction(conn):
            conn.execute("INSERT INTO items VALUES ('a', 1)")
            raise RuntimeError("中止")
    assert not conn.in_transaction
    assert names(conn) == []


def test_nested_transaction_rolls_back_only_the_inner_part(conn):
    with transaction(conn):
        conn.execute("INSERT INTO items VALUES ('outer', 1)")
        with pytest.raises(ValueError):
            with transaction(conn):
                conn.execute("INSERT INTO items VALUES ('inner', 2)")
                raise ValueError("内层失败")
        with transaction(conn):
            conn.execute("INSERT INTO items VALUES ('second', 3)")
    assert names(conn) == ["outer", "second"]


def test_readers_are_not_blocked_by_an_open_write_transaction(conn, tmp_path):
    conn.execute("INSERT INTO items VALUES ('a', 1)")
    reader = connect(tmp_path / "data" / "tightrein.db")
    with transaction(conn):
        conn.execute("INSERT INTO items VALUES ('b', 2)")
        assert names(reader) == ["a"]
    assert names(reader) == ["a", "b"]
    reader.close()


def test_a_second_writer_waits_for_the_busy_timeout_then_fails(conn, tmp_path):
    writer = connect(tmp_path / "data" / "tightrein.db", busy_timeout_ms=50)
    with transaction(conn):
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with transaction(writer):
                pass
    assert not writer.in_transaction
    writer.close()
