import sqlite3
from datetime import timedelta

from tightrein.protocol.naming import FixedClock, run_started
from tightrein.store.files.json import write_json
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.retention import purge
from tightrein.store.tables import operations, runs

from .conftest import NOW

DAY = 86400.0
POLICY = {"runs": 90 * DAY, "raw": 30 * DAY, "operations": 30 * DAY}
OLD_RUN = "R-20260601T000000Z-collect"  # 128 天前
MIDDLE_RUN = "R-20260901T000000Z-implement"  # 36 天前
NEW_RUN = "R-20261001T000000Z-collect"  # 6 天前


def make_run_dir(layout: WorkspaceLayout, run: str) -> None:
    directory = layout.run_dir(run)
    directory.mkdir(parents=True)
    (directory / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (directory / "12-collect.platform_errors-handoff.json").write_text("{}\n", encoding="utf-8")
    (directory / "12-collect.platform_errors-raw.json").write_text("{}\n", encoding="utf-8")
    (directory / "12-collect.platform_errors-prompt.md").write_text("p\n", encoding="utf-8")


def test_old_run_directories_and_records_are_deleted(
    layout: WorkspaceLayout, conn: sqlite3.Connection, clock: FixedClock
) -> None:
    for run in (OLD_RUN, MIDDLE_RUN, NEW_RUN):
        make_run_dir(layout, run)
        stage = run.rsplit("-", 1)[1]
        runs.start(conn, runs.Run(run, stage, "schedule", "done", run_started(run)))
    (layout.runs_dir / "notes").mkdir()
    removed = purge(layout, conn, clock, POLICY)
    assert removed["runs"] == 1
    assert sorted(path.name for path in layout.runs_dir.iterdir()) == [MIDDLE_RUN, NEW_RUN, "notes"]
    assert [run.id for run in runs.TABLE.find(conn)] == [MIDDLE_RUN, NEW_RUN]


def test_a_running_run_is_kept(layout: WorkspaceLayout, conn: sqlite3.Connection, clock: FixedClock) -> None:
    make_run_dir(layout, OLD_RUN)
    runs.start(conn, runs.Run(OLD_RUN, "collect", "schedule", "running", run_started(OLD_RUN)))
    assert purge(layout, conn, clock, {"runs": 90 * DAY})["runs"] == 0
    assert layout.run_dir(OLD_RUN).is_dir()
    assert runs.get(conn, OLD_RUN) is not None


def test_old_raw_output_and_prompts_are_deleted(
    layout: WorkspaceLayout, conn: sqlite3.Connection, clock: FixedClock
) -> None:
    make_run_dir(layout, MIDDLE_RUN)
    make_run_dir(layout, NEW_RUN)
    issue = layout.issue_dir("0018")
    write_json(issue / "33-implement.design-handoff.json", {"run": MIDDLE_RUN})
    (issue / "33-implement.design-prompt.md").write_text("p\n", encoding="utf-8")
    (issue / "33-implement.design-raw.json").write_text("{}\n", encoding="utf-8")
    write_json(issue / "35-implement.code.r1-handoff.json", {"run": NEW_RUN})
    (issue / "35-implement.code.r1-raw.json").write_text("{}\n", encoding="utf-8")
    (issue / "36-implement.check.r1-raw.json").write_text("{}\n", encoding="utf-8")  # 没有交接：不知道年龄，保留
    removed = purge(layout, conn, clock, {"raw": 30 * DAY})
    assert removed == {"raw": 4}
    assert sorted(path.name for path in layout.run_dir(MIDDLE_RUN).iterdir()) == [
        "12-collect.platform_errors-handoff.json", "events.jsonl"]
    assert len(list(layout.run_dir(NEW_RUN).iterdir())) == 4
    assert sorted(path.name for path in issue.iterdir()) == [
        "33-implement.design-handoff.json", "35-implement.code.r1-handoff.json", "35-implement.code.r1-raw.json",
        "36-implement.check.r1-raw.json"]


def test_only_old_finished_operations_are_deleted(
    layout: WorkspaceLayout, conn: sqlite3.Connection, clock: FixedClock
) -> None:
    operations.run_once(conn, "push:0018:old", lambda: {"ok": True}, clock)
    conn.execute("INSERT INTO operations (\"key\", status, created_at, updated_at) "
                 "VALUES ('pr:0018:stuck', 'in_progress', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
    clock.advance(timedelta(days=31))
    operations.run_once(conn, "push:0018:new", lambda: {"ok": True}, clock)
    assert purge(layout, conn, clock, POLICY)["operations"] == 1
    assert [item.key for item in operations.find(conn)] == ["pr:0018:stuck", "push:0018:new"]


def test_nothing_happens_without_a_policy(layout: WorkspaceLayout, conn: sqlite3.Connection) -> None:
    make_run_dir(layout, OLD_RUN)
    assert purge(layout, conn, FixedClock(NOW), {}) == {}
    assert layout.run_dir(OLD_RUN).is_dir()

