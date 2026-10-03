from datetime import timedelta

import pytest

from tightrein.domain.enums import (
    Disposition,
    ProblemEvent,
    ProblemStatus,
    Verdict,
)
from tightrein.domain.triage import TriageResult
from tightrein.store import retention
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import problem_events, problems, runs, signals, triage
from tightrein.store.repos.problem_events import OPERATION_AUTO, ProblemEventRecord
from tightrein.store.repos.triage import TriageRecord
from tightrein.store.retention import RetentionPolicy, RetentionReport

from store_samples import problem, run, signal

DAY = timedelta(days=1)


@pytest.fixture
def layout(tmp_path):
    return WorkspaceLayout(tmp_path / "sample")


def resolved(problem_id, fingerprint, resolved_at, **changes):
    return problem(problem_id, fingerprint, status=ProblemStatus.RESOLVED, first_seen_at=resolved_at - DAY,
                   last_seen_at=resolved_at - DAY, **changes)


def resolve_event(conn, problem_id, at):
    problem_events.append(conn, ProblemEventRecord(problem_id, at, ProblemEvent.COVERED_RUN_WITHOUT_OCCURRENCE,
                                                   OPERATION_AUTO, ProblemStatus.ONGOING, ProblemStatus.RESOLVED))


def test_old_signals_are_deleted_and_problem_counts_kept(conn, layout, clock):
    now = clock.now()
    runs.save(conn, run())
    old, recent = signal(1, occurred_at=now - 91 * DAY), signal(2, occurred_at=now - 89 * DAY)
    signals.save_all(conn, [old, recent])
    problems.save(conn, problem(occurrences=2, first_seen_at=now - 91 * DAY, last_seen_at=now - 89 * DAY))
    problems.add_signals(conn, "P-0001", [old.id, recent.id])
    report = retention.purge(conn, layout, clock)
    assert report == RetentionReport(1, (), (), ())
    assert [item.id for item in signals.find(conn)] == [recent.id]
    assert problems.signal_ids(conn, "P-0001") == [recent.id]
    assert problems.get(conn, "P-0001").occurrences == 2


def test_long_resolved_problems_without_issue_are_deleted_with_their_records(conn, layout, clock):
    now = clock.now()
    runs.save(conn, run())
    signals.save_all(conn, [signal(1, occurred_at=now - DAY)])
    problems.save(conn, resolved("P-0001", "f1", now - 100 * DAY))
    resolve_event(conn, "P-0001", now - 91 * DAY)
    problems.save(conn, problem("P-0002", "f2", merged_into="P-0001"))
    problems.save(conn, resolved("P-0003", "f3", now - 100 * DAY, issue_id="0007"))
    problems.save(conn, resolved("P-0004", "f4", now - 100 * DAY))
    resolve_event(conn, "P-0004", now - 10 * DAY)
    problems.save(conn, resolved("P-0005", "f5", now - 92 * DAY))
    problems.save(conn, problem("P-0006", "f6", status=ProblemStatus.ONGOING, last_seen_at=now - 200 * DAY,
                                first_seen_at=now - 200 * DAY))
    problems.add_signals(conn, "P-0001", [signal(1).id])
    problems.add_alias(conn, "f1-old", "P-0001", clock)
    for problem_id in ("P-0001", "P-0002", "P-0004"):
        triage.save(conn, TriageRecord(TriageResult(problem_id, 1, Verdict.CONFIRMED, Disposition.CREATE_ISSUE,
                                                    "r", "abc1234"), "R-20260601-000000-triage", now - 120 * DAY))

    report = retention.purge(conn, layout, clock)

    assert report.problems == ("P-0001", "P-0002", "P-0005")
    assert [item.id for item in problems.find(conn)] == ["P-0003", "P-0004", "P-0006"]
    assert problems.by_fingerprint(conn, "f1-old") is None
    assert problem_events.for_problem(conn, "P-0001") == []
    assert [row[0] for row in conn.execute("SELECT problem_id FROM triage_results")] == ["P-0004"]
    assert [item.id for item in signals.find(conn)] == [signal(1).id]


def test_old_raw_output_and_transcripts_are_deleted(conn, layout, clock):
    old_run, new_run = "R-20260901-010000-collect-api-fuzz", "R-20260920-010000-triage"
    for run_id in (old_run, new_run):
        for path in (layout.probe_raw_dir(run_id, run().probe) / "report.xml",
                     layout.transcript(run_id, "judge", "0007"),
                     layout.handoff(run_id, "collect-x"), layout.signals_file(run_id)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
    (layout.runs_dir() / "notes").mkdir()
    report = retention.purge(conn, layout, clock)
    assert report.run_files == (layout.raw_dir(old_run), layout.transcripts_dir(old_run))
    assert not layout.raw_dir(old_run).exists() and not layout.transcripts_dir(old_run).exists()
    assert layout.handoff(old_run, "collect-x").exists() and layout.signals_file(old_run).exists()
    assert layout.raw_dir(new_run).exists() and layout.transcripts_dir(new_run).exists()


def test_old_event_logs_are_deleted(conn, layout, clock):
    today = clock.now().date()
    kept = [layout.events_log(today - timedelta(days=90)), layout.launchd_out_log()]
    removed = layout.events_log(today - timedelta(days=91))
    for path in (*kept, removed):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    assert retention.purge(conn, layout, clock).logs == (removed,)
    assert all(path.exists() for path in kept)


def test_policy_changes_the_periods(conn, layout, clock):
    runs.save(conn, run())
    signals.save_all(conn, [signal(1, occurred_at=clock.now() - 10 * DAY)])
    path = layout.events_log(clock.now().date() - timedelta(days=8))
    path.parent.mkdir(parents=True)
    path.write_text("{}\n", encoding="utf-8")
    report = retention.purge(conn, layout, clock, RetentionPolicy(signals_days=7, raw_days=7, logs_days=7))
    assert (report.signals, report.logs) == (1, (path,))


def test_an_empty_workspace_has_nothing_to_purge(conn, layout, clock):
    assert retention.purge(conn, layout, clock) == RetentionReport(0, (), (), ())
