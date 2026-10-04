import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone

import pytest

from tightrein.contracts.validate import SchemaValidationError
from tightrein.domain.enums import Probe, RunStage, RunStatus, SignalAggregateState, Stage
from tightrein.domain.run import Coverage, Run
from tightrein.store import locks
from tightrein.store.repos import runs, signals
from tightrein.store.repos.table import DATE, JSON, TIME, Table, enum_codec, given, where

from store_samples import RUN_ID, T0, run, signal


@dataclass(frozen=True)
class Note:
    day: date
    stage: Stage
    data: dict
    at: datetime | None = None
    id: int | None = None


NOTES = Table("notes", Note, ("id",), {"day": DATE, "stage": enum_codec(Stage), "data": JSON, "at": TIME},
              order_by="day, id", generated=("id",))


def test_table_round_trip_with_codecs_and_generated_keys(conn):
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, day TEXT, stage TEXT, data TEXT, at TEXT)")
    first = NOTES.insert(conn, Note(date(2026, 10, 2), Stage.FIX, {"a": [1, "二"]}))
    second = NOTES.insert(conn, Note(date(2026, 10, 1), Stage.FIX, {}, T0))
    assert NOTES.get(conn, id=first) == Note(date(2026, 10, 2), Stage.FIX, {"a": [1, "二"]}, None, first)
    assert [note.id for note in NOTES.find(conn, stage=Stage.FIX)] == [second, first]
    assert [note.id for note in NOTES.find(conn, at=None)] == [first]
    NOTES.save(conn, Note(date(2026, 10, 3), Stage.VERIFY, {}, None, first))
    assert NOTES.get(conn, id=first).stage is Stage.VERIFY
    assert NOTES.delete(conn, id=second) == 1
    assert NOTES.get(conn, id=second) is None
    with pytest.raises(ValueError):
        NOTES.delete(conn)


def test_where_and_given():
    assert where({"a": 1, "b": None}) == (' WHERE "a" = ? AND "b" IS NULL', [1])
    assert where({}) == ("", [])
    assert given({"a": 1, "b": None}) == {"a": 1}


def test_run_round_trip(conn):
    runs.save(conn, run())
    assert runs.get(conn, RUN_ID) == run()
    assert runs.get(conn, "R-20260929-000000-aggregate") is None


def test_run_without_probe_uses_defaults(conn):
    aggregate = Run("R-20260929-030000-aggregate", RunStage.AGGREGATE, T0, RunStatus.OK)
    runs.save(conn, aggregate)
    assert runs.get(conn, aggregate.id) == aggregate


def test_a_new_running_run_records_the_process_that_started_it(conn):
    holder = locks.current_holder()
    started = Run("R-20260929-030000-aggregate", RunStage.AGGREGATE, T0, RunStatus.RUNNING)
    runs.save(conn, started)
    saved = runs.get(conn, started.id)
    assert (saved.holder_pid, saved.holder_host) == (holder.pid, holder.host)
    assert saved == replace(started, holder_pid=holder.pid, holder_host=holder.host)
    # 之后以内存中没有进程号的对象保存(模块结束运行时)不会清掉进程号
    runs.save(conn, replace(started, status=RunStatus.OK, ended_at=T0))
    assert runs.get(conn, started.id).holder_pid == holder.pid
    assert [item.id for item in runs.find(conn, holder_pid=holder.pid, holder_host=holder.host)] == [started.id]
    assert runs.find(conn, holder_pid=holder.pid + 1) == []


def test_runs_saved_finished_or_from_before_the_migration_have_no_process(conn):
    runs.save(conn, run())
    assert (runs.get(conn, RUN_ID).holder_pid, runs.get(conn, RUN_ID).holder_host) == (None, None)


def test_saving_again_updates_the_run(conn):
    runs.save(conn, run(status=RunStatus.RUNNING, ended_at=None))
    runs.save(conn, run())
    assert runs.get(conn, RUN_ID).status is RunStatus.OK
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_invalid_coverage_is_rejected_before_writing(conn):
    with pytest.raises(SchemaValidationError, match=r"\$\.sources\[0\]"):
        runs.save(conn, run(coverage=Coverage(sources=("",))))
    assert runs.get(conn, RUN_ID) is None


def test_find_filters_and_orders_by_start(conn):
    later = run("R-20260929-031500-collect-alerts", probe=Probe.ALERTS, started_at=T0 + timedelta(hours=1))
    loop = Run("R-20260929-020000-loop", RunStage.LOOP, T0 - timedelta(hours=1), RunStatus.OK)
    child = run(parent_run_id=loop.id)
    for item in (later, loop, child):
        runs.save(conn, item)
    assert [item.id for item in runs.find(conn, stage=RunStage.COLLECT)] == [RUN_ID, later.id]
    assert [item.id for item in runs.find(conn, probe=Probe.ALERTS)] == [later.id]
    assert [item.id for item in runs.find(conn, parent_run_id=loop.id)] == [RUN_ID]
    assert [item.id for item in runs.find(conn)] == [loop.id, RUN_ID, later.id]


def test_unaggregated_and_mark_aggregated(conn):
    running = run("R-20260929-031500-collect-alerts", probe=Probe.ALERTS, status=RunStatus.RUNNING)
    done = run("R-20260929-011500-collect-static", probe=Probe.STATIC, started_at=T0 - timedelta(hours=1),
               aggregated_at=T0)
    for item in (run(), running, done):
        runs.save(conn, item)
    assert [item.id for item in runs.unaggregated(conn)] == [RUN_ID]
    runs.mark_aggregated(conn, [RUN_ID], T0 + timedelta(minutes=5))
    assert runs.unaggregated(conn) == []
    assert runs.get(conn, RUN_ID).aggregated_at == T0 + timedelta(minutes=5)


def test_signal_round_trip(conn):
    item = signal(context={"response": {"status": 500}, "说明": "中文"}, normalized_message="500 <value>",
                  fingerprint="a1b2c3d4e5f60718", suppressed=True, aggregate_state=SignalAggregateState.DONE)
    signals.save(conn, item)
    assert signals.get(conn, item.id) == item
    assert conn.execute('SELECT "check" FROM signals').fetchone()[0] == "not_a_server_error"


def test_save_all_is_atomic(conn):
    with pytest.raises(sqlite3.IntegrityError):
        signals.save_all(conn, [signal(1), signal(2, run_id=None)])
    assert signals.find(conn) == []
    signals.save_all(conn, [signal(1), signal(2)])
    assert len(signals.find(conn)) == 2


def test_find_signals_by_conditions(conn):
    first = signal(1, occurred_at=T0 + timedelta(seconds=5), fingerprint="f1")
    second = signal(2, probe=Probe.PLATFORM_ERRORS, fingerprint="f1", aggregate_state=SignalAggregateState.VOIDED)
    third = signal(3, run_id="R-20260929-031500-collect-alerts", occurred_at=datetime(2026, 9, 30, tzinfo=timezone.utc))
    signals.save_all(conn, [first, second, third])
    assert [item.id for item in signals.find(conn, fingerprint="f1")] == [second.id, first.id]
    assert [item.id for item in signals.find(conn, run_id=RUN_ID, probe=Probe.API_FUZZ)] == [first.id]
    assert [item.id for item in signals.find(conn, aggregate_state=SignalAggregateState.VOIDED)] == [second.id]
    assert [item.id for item in signals.get_many(conn, [third.id, first.id, "S-missing"])] == [first.id, third.id]
    assert signals.get_many(conn, []) == []


def test_free_id_moves_to_the_next_second_when_the_id_is_taken(conn):
    started = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
    first = runs.free_id(conn, started, RunStage.TRIAGE)
    assert first == "R-20261005-030000-triage"
    runs.save(conn, Run(first, RunStage.TRIAGE, started, RunStatus.OK))
    assert runs.free_id(conn, started, RunStage.TRIAGE) == "R-20261005-030001-triage"
    assert runs.free_id(conn, started, RunStage.ISSUE) == "R-20261005-030000-issue"
