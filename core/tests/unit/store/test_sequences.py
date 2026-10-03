import threading

import pytest

from tightrein.domain.enums import KnowledgeType
from tightrein.store import sequences
from tightrein.store.db import connect, transaction


def test_values_start_at_one_and_increase(conn):
    assert sequences.current(conn, "problem") == 0
    assert [sequences.next_value(conn, "problem") for _ in range(3)] == [1, 2, 3]
    assert sequences.current(conn, "problem") == 3
    assert sequences.next_value(conn, "issue") == 1


def test_ids_use_the_domain_formats(conn):
    assert sequences.next_issue_id(conn) == "0001"
    assert sequences.next_operation_id(conn) == "OP-0001"
    assert sequences.next_suggestion_id(conn) == "LS-0001"
    assert sequences.next_knowledge_id(conn, KnowledgeType.DEFECT_PATTERN) == "DP-0001"
    assert sequences.next_knowledge_id(conn, KnowledgeType.DEFECT_PATTERN) == "DP-0002"
    assert sequences.next_knowledge_id(conn, KnowledgeType.FIX_LESSON) == "FL-0001"
    assert sequences.current(conn, "knowledge-DP") == 2


def test_ensure_at_least_only_moves_forward(conn):
    sequences.ensure_at_least(conn, "issue", 7)
    assert sequences.next_issue_id(conn) == "0008"
    sequences.ensure_at_least(conn, "issue", 3)
    assert sequences.current(conn, "issue") == 8


def test_a_rolled_back_allocation_is_not_used(conn):
    with pytest.raises(RuntimeError):
        with transaction(conn):
            assert sequences.next_value(conn, "problem") == 1
            raise RuntimeError("回滚")
    assert sequences.next_value(conn, "problem") == 1


def test_concurrent_allocations_never_repeat(conn, db_path):
    results: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def allocate() -> None:
        connection = connect(db_path)
        try:
            values = [sequences.next_value(connection, "problem") for _ in range(50)]
        except BaseException as error:
            errors.append(error)
            return
        finally:
            connection.close()
        with lock:
            results.extend(values)

    threads = [threading.Thread(target=allocate) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(results) == list(range(1, 401))
