import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from tightrein.protocol.naming import FixedClock
from tightrein.store.db import (
    MigrationError,
    applied,
    connect,
    discover,
    migrate,
    open_database,
    transaction,
)

from .conftest import NOW

TABLES = {"problems", "occurrences", "issues", "runs", "operations", "counters", "state", "sequences"}


@pytest.fixture
def items(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "data" / "tightrein.db")
    connection.execute("CREATE TABLE items (name TEXT PRIMARY KEY, value INTEGER)")
    yield connection
    connection.close()


@pytest.fixture
def empty(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "tightrein.db")
    yield connection
    connection.close()


def names(connection: sqlite3.Connection) -> list[str]:
    return [row["name"] for row in connection.execute("SELECT name FROM items ORDER BY name")]


def columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]


def write_migration(directory: Path, name: str, sql: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(sql, encoding="utf-8")


# 连接与事务


def test_connect_creates_the_directory_and_enables_wal(tmp_path: Path) -> None:
    connection = connect(tmp_path / "a" / "b" / "tightrein.db")
    assert (tmp_path / "a" / "b" / "tightrein.db").exists()
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    connection.close()


def test_connect_sets_busy_timeout_and_foreign_keys(tmp_path: Path) -> None:
    connection = connect(tmp_path / "tightrein.db")
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    connection.close()
    other = connect(tmp_path / "tightrein.db", busy_timeout_ms=250)
    assert other.execute("PRAGMA busy_timeout").fetchone()[0] == 250
    other.close()


def test_statements_outside_a_transaction_commit_immediately(items: sqlite3.Connection, tmp_path: Path) -> None:
    items.execute("INSERT INTO items VALUES ('a', 1)")
    other = connect(tmp_path / "data" / "tightrein.db")
    assert names(other) == ["a"]
    assert other.execute("SELECT value FROM items").fetchone()["value"] == 1
    other.close()


def test_transaction_rolls_back_on_error(items: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError), transaction(items):
        items.execute("INSERT INTO items VALUES ('a', 1)")
        raise RuntimeError("中止")
    assert not items.in_transaction
    assert names(items) == []


def test_nested_transaction_rolls_back_only_the_inner_part(items: sqlite3.Connection) -> None:
    with transaction(items):
        items.execute("INSERT INTO items VALUES ('outer', 1)")
        with pytest.raises(ValueError), transaction(items):
            items.execute("INSERT INTO items VALUES ('inner', 2)")
            raise ValueError("内层失败")
        with transaction(items):
            items.execute("INSERT INTO items VALUES ('second', 3)")
    assert names(items) == ["outer", "second"]


def test_readers_are_not_blocked_by_an_open_write_transaction(items: sqlite3.Connection, tmp_path: Path) -> None:
    items.execute("INSERT INTO items VALUES ('a', 1)")
    reader = connect(tmp_path / "data" / "tightrein.db")
    with transaction(items):
        items.execute("INSERT INTO items VALUES ('b', 2)")
        assert names(reader) == ["a"]
    assert names(reader) == ["a", "b"]
    reader.close()


def test_a_second_writer_waits_for_the_busy_timeout_then_fails(items: sqlite3.Connection, tmp_path: Path) -> None:
    writer = connect(tmp_path / "data" / "tightrein.db", busy_timeout_ms=50)
    with transaction(items), pytest.raises(sqlite3.OperationalError, match="locked"), transaction(writer):
        pass
    assert not writer.in_transaction
    writer.close()


# 迁移


def test_open_database_creates_the_eight_tables_once(tmp_path: Path, clock: FixedClock) -> None:
    first = open_database(tmp_path / "tightrein.db", clock=clock)
    tables = {row[0] for row in first.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert TABLES <= tables
    assert [tuple(row) for row in first.execute("SELECT * FROM schema_migrations")] == [
        (1, "schema", "2026-10-07T09:30:00Z")]
    first.close()
    second = open_database(tmp_path / "tightrein.db", clock=clock)
    assert applied(second) == {1: "schema"}
    second.close()


def test_migrate_again_does_nothing(empty: sqlite3.Connection, clock: FixedClock) -> None:
    assert migrate(empty, clock) == [1]
    empty.execute("INSERT INTO sequences (name, value) VALUES ('problem', 3)")
    assert migrate(empty, clock) == []
    assert empty.execute("SELECT value FROM sequences").fetchone()[0] == 3


def test_tables_are_strict(empty: sqlite3.Connection, clock: FixedClock) -> None:
    migrate(empty, clock)
    with pytest.raises(sqlite3.IntegrityError):
        empty.execute("INSERT INTO sequences (name, value) VALUES ('problem', 'many')")


def test_occurrences_go_with_their_problem(empty: sqlite3.Connection, clock: FixedClock) -> None:
    migrate(empty, clock)
    empty.execute(
        "INSERT INTO problems (id, fingerprint, source, check_type, status, title, first_seen, last_seen, "
        "created_at, updated_at) VALUES ('P-0001', 'f', 's', 'c', 'new', 't', 'x', 'x', 'x', 'x')"
    )
    occurrence = (
        "INSERT INTO occurrences (problem, seen_at, source, created_at, updated_at) VALUES (?, 'x', 's', 'x', 'x')"
    )
    empty.execute(occurrence, ("P-0001",))
    with pytest.raises(sqlite3.IntegrityError):
        empty.execute(occurrence, ("P-0404",))
    empty.execute("DELETE FROM problems")
    assert empty.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0] == 0


def test_later_migrations_follow_the_schema(tmp_path: Path, empty: sqlite3.Connection, clock: FixedClock) -> None:
    directory = tmp_path / "migrations"
    write_migration(directory, "002_issue_labels.sql", "ALTER TABLE issues ADD COLUMN labels TEXT;")
    assert migrate(empty, clock, directory) == [1, 2]
    assert applied(empty) == {1: "schema", 2: "issue_labels"}
    assert "labels" in columns(empty, "issues")


def test_a_failed_migration_is_rolled_back(tmp_path: Path, empty: sqlite3.Connection, clock: FixedClock) -> None:
    directory = tmp_path / "migrations"
    write_migration(directory, "002_first.sql", "CREATE TABLE a (x INTEGER);")
    write_migration(directory, "003_broken.sql", "CREATE TABLE b (x INTEGER);\nINSERT INTO missing VALUES (1);")
    with pytest.raises(MigrationError, match="003_broken.sql"):
        migrate(empty, clock, directory)
    assert applied(empty) == {1: "schema", 2: "first"}
    assert columns(empty, "a") == ["x"]
    assert columns(empty, "b") == []
    assert not empty.in_transaction


def test_unknown_or_renamed_versions_in_the_database_are_rejected(
    empty: sqlite3.Connection, clock: FixedClock
) -> None:
    migrate(empty, clock)
    empty.execute("INSERT INTO schema_migrations VALUES (99, 'later', 'x')")
    with pytest.raises(MigrationError, match=r"\[99\]"):
        migrate(empty, clock)
    empty.execute("DELETE FROM schema_migrations WHERE version = 99")
    empty.execute("UPDATE schema_migrations SET name = 'renamed'")
    with pytest.raises(MigrationError, match="renamed"):
        migrate(empty, clock)


def test_discover_rejects_bad_names_and_duplicates(tmp_path: Path) -> None:
    write_migration(tmp_path / "bad", "2_labels.sql", "")
    with pytest.raises(MigrationError, match="2_labels.sql"):
        discover(tmp_path / "bad")
    write_migration(tmp_path / "dup", "002_a.sql", "")
    write_migration(tmp_path / "dup", "002_b.sql", "")
    with pytest.raises(MigrationError, match="重复"):
        discover(tmp_path / "dup")
    write_migration(tmp_path / "first", "001_again.sql", "")
    with pytest.raises(MigrationError, match="重复"):
        discover(tmp_path / "first")
    assert [migration.name for migration in discover(tmp_path / "missing")] == ["schema"]


def test_migrate_refuses_to_run_inside_a_transaction(empty: sqlite3.Connection) -> None:
    empty.execute("BEGIN")
    with pytest.raises(MigrationError, match="事务"):
        migrate(empty, FixedClock(NOW))
    empty.execute("ROLLBACK")
