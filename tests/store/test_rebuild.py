import sqlite3
from collections.abc import Iterator

import pytest

from tightrein.protocol.naming import FixedClock, parse_iso
from tightrein.store import rebuild
from tightrein.store.files.json import write_json
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.rebuild import RebuildError, register
from tightrein.store.tables import counters, runs, sequences

DONE_RUN = "R-20261006T010000Z-collect"
FAILED_RUN = "R-20261006T020000Z-implement"
EMPTY_RUN = "R-20261006T030000Z-collect"


@pytest.fixture(autouse=True)
def registry() -> Iterator[None]:
    saved = dict(rebuild.REBUILDERS)
    yield
    rebuild.REBUILDERS.clear()
    rebuild.REBUILDERS.update(saved)


def handoff(run: str, status: str, created: str) -> dict[str, str]:
    return {"run": run, "status": status, "createdAt": created}


def make_files(layout: WorkspaceLayout) -> None:
    for run in (DONE_RUN, FAILED_RUN, EMPTY_RUN):
        layout.run_dir(run).mkdir(parents=True)
    write_json(layout.run_dir(DONE_RUN) / "12-collect.platform_errors-handoff.json",
               handoff(DONE_RUN, "passed", "2026-10-06T01:05:00Z"))
    write_json(layout.run_dir(DONE_RUN) / "19-collect.dedup-handoff.json",
               handoff(DONE_RUN, "passed", "2026-10-06T01:09:00Z"))
    write_json(layout.issue_dir("0018") / "33-implement.design-handoff.json",
               handoff(FAILED_RUN, "passed", "2026-10-06T02:10:00Z"))
    write_json(layout.issue_dir("0018") / "35-implement.code.r1-handoff.json",
               handoff(FAILED_RUN, "failed", "2026-10-06T02:30:00Z"))


def test_runs_are_rebuilt_from_the_run_directories(
    layout: WorkspaceLayout, conn: sqlite3.Connection, clock: FixedClock
) -> None:
    make_files(layout)
    runs.start(conn, runs.Run("R-20261001T000000Z-collect", "collect", "manual", "done", clock.now()))
    assert rebuild.rebuild(layout, conn) == {"runs": 3, "problems": 0, "issues": 0}
    found = {run.id: (run.stage, run.status, run.ended_at) for run in runs.TABLE.find(conn)}
    assert found == {
        DONE_RUN: ("collect", "done", parse_iso("2026-10-06T01:09:00Z")),
        FAILED_RUN: ("implement", "failed", parse_iso("2026-10-06T02:30:00Z")),
        EMPTY_RUN: ("collect", "interrupted", None),
    }


def test_a_broken_file_changes_nothing_and_lists_every_error(
    layout: WorkspaceLayout, conn: sqlite3.Connection, clock: FixedClock
) -> None:
    make_files(layout)
    (layout.run_dir(DONE_RUN) / "11-collect.project_probes-handoff.json").write_text("{", encoding="utf-8")
    write_json(layout.issue_dir("0019") / "21-assess.triage-handoff.json", {"status": "passed"})
    runs.start(conn, runs.Run(DONE_RUN, "collect", "schedule", "running", clock.now()))
    with pytest.raises(RebuildError) as error:
        rebuild.rebuild(layout, conn)
    assert len(error.value.errors) == 2
    assert [run.trigger for run in runs.TABLE.find(conn)] == ["schedule"]


def test_registered_tables_are_rebuilt_and_sequences_moved_past_the_files(
    layout: WorkspaceLayout, conn: sqlite3.Connection, clock: FixedClock
) -> None:
    def issues_from_files(layout: WorkspaceLayout, conn: sqlite3.Connection) -> int:
        for issue in ("0003", "0017"):
            conn.execute(
                "INSERT INTO issues (id, status, title, kind, origin, created_at, updated_at) "
                "VALUES (?, 'todo', 't', 'bug', 'user', 'x', 'x')", (issue,)
            )
        return 2

    register("issues", issues_from_files)
    counters.add(conn, "budget", 5, clock)
    assert rebuild.rebuild(layout, conn) == {"runs": 0, "problems": 0, "issues": 2}
    assert sequences.next_value(conn, sequences.ISSUE) == 18
    assert counters.get(conn, "budget") == 5  # 计数没有对应的文件，不清空
