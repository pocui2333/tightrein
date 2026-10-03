import sqlite3
from datetime import date, timedelta

import pytest

from tightrein.domain.enums import (
    HandoffStatus,
    RunStage,
    ScoreMethod,
    ScoreResult,
    Stage,
    SuggestionKind,
    SuggestionStatus,
    VerifyPhase,
)
from tightrein.store.repos import handoffs, metric_snapshots, scores, suggestions
from tightrein.store.repos.handoffs import HandoffRecord
from tightrein.store.repos.metric_snapshots import OVERALL, MetricSnapshot
from tightrein.store.repos.scores import ScoreRecord
from tightrein.store.repos.suggestions import SuggestionRecord

from store_samples import T0

RUN = "R-20260929-021503-triage"


def handoff(stage=RunStage.TRIAGE, subject="P-0001", attempt=1, phase=None, **changes):
    values = dict(stage=stage, subject_id=subject, attempt=attempt, run_id=RUN,
                  path=f"data/runs/{RUN}/handoff/{stage.value}-{subject}.json", status=HandoffStatus.OK,
                  schema_version=1, created_at=T0, phase=phase)
    values.update(changes)
    return HandoffRecord(**values)


def test_handoff_round_trip_and_phase(conn):
    handoffs.save(conn, handoff())
    verify = handoff(RunStage.VERIFY, "0007", phase=VerifyPhase.LOCAL)
    handoffs.save(conn, verify)
    assert handoffs.get(conn, RunStage.TRIAGE, "P-0001") == handoff()
    assert handoffs.get(conn, RunStage.VERIFY, "0007", phase=VerifyPhase.LOCAL) == verify
    assert handoffs.get(conn, RunStage.VERIFY, "0007", phase=VerifyPhase.STAGING) is None
    assert conn.execute("SELECT phase FROM handoffs WHERE stage = 'triage'").fetchone()[0] == ""
    with pytest.raises(ValueError):
        handoff(RunStage.VERIFY, "0007")
    with pytest.raises(ValueError):
        handoff(phase=VerifyPhase.LOCAL)


def test_handoff_rerun_updates_the_same_key(conn):
    handoffs.save(conn, handoff())
    handoffs.save(conn, handoff(run_id="R-20260930-000000-triage", status=HandoffStatus.BLOCKED))
    handoffs.save(conn, handoff(attempt=2, created_at=T0 + timedelta(hours=1)))
    assert conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 2
    assert handoffs.get(conn, RunStage.TRIAGE, "P-0001").attempt == 2
    assert handoffs.get(conn, RunStage.TRIAGE, "P-0001", attempt=1).status is HandoffStatus.BLOCKED
    assert [item.attempt for item in handoffs.for_subject(conn, "P-0001")] == [1, 2]
    assert [item.attempt for item in handoffs.for_run(conn, RUN)] == [2]


def test_mark_stale_only_touches_fresh_records(conn):
    handoffs.save(conn, handoff())
    handoffs.save(conn, handoff(attempt=2))
    handoffs.save(conn, handoff(RunStage.ISSUE))
    assert handoffs.mark_stale(conn, RunStage.TRIAGE, "P-0001", T0) == 2
    assert handoffs.mark_stale(conn, RunStage.TRIAGE, "P-0001", T0 + timedelta(hours=1)) == 0
    assert handoffs.get(conn, RunStage.TRIAGE, "P-0001").stale_at == T0
    assert handoffs.get(conn, RunStage.ISSUE, "P-0001").stale_at is None


def test_scores_append_and_find(conn):
    first = scores.append(conn, ScoreRecord(Stage.TRIAGE, RUN, "P-0001", 1, "T-01", ScoreResult.PASS,
                                            ScoreMethod.CODE, T0))
    second = scores.append(conn, ScoreRecord(Stage.TRIAGE, RUN, "P-0002", 1, "T-02", ScoreResult.UNKNOWN,
                                             ScoreMethod.JUDGE, T0, "证据不足"))
    assert [item.id for item in scores.find(conn, stage=Stage.TRIAGE, run_id=RUN)] == [first, second]
    assert scores.find(conn, item="T-02")[0].detail == "证据不足"
    assert scores.find(conn, subject_id="P-0404") == []


def test_suggestions_round_trip_and_find(conn):
    pending = SuggestionRecord("LS-0021", SuggestionKind.IMPROVEMENT, "prompt:fix", SuggestionStatus.PENDING, T0,
                               {"runs": [RUN]}, "data/improve/LS-0021.md", "+ 新条目", "0" * 64)
    decided = SuggestionRecord("LS-0022", SuggestionKind.CONTROL, "gate:merge", SuggestionStatus.REJECTED, T0,
                               reason="重复", decided_at=T0 + timedelta(days=1))
    suggestions.save(conn, pending)
    suggestions.save(conn, decided)
    assert suggestions.get(conn, "LS-0021") == pending
    assert suggestions.find(conn, status=SuggestionStatus.REJECTED) == [decided]
    assert suggestions.find(conn, kind=SuggestionKind.PROBE_CONFIG) == []


def test_metric_snapshots_by_week_and_series(conn):
    weeks = [date(2026, 9, 21), date(2026, 9, 28), date(2026, 10, 5)]
    for index, week in enumerate(weeks):
        metric_snapshots.save(conn, MetricSnapshot(week, "false-confirm-rate", OVERALL, 10, T0, 0.1 * index, index, 10))
    metric_snapshots.save(conn, MetricSnapshot(weeks[2], "false-confirm-rate", "api-fuzz", 4, T0, 0.25, 1, 4))
    metric_snapshots.save(conn, MetricSnapshot(weeks[2], "false-confirm-rate", OVERALL, 12, T0, 0.5, 6, 12))
    assert [item.value for item in metric_snapshots.series(conn, "false-confirm-rate")] == [0.0, 0.1, 0.5]
    assert [item.week for item in metric_snapshots.series(conn, "false-confirm-rate", since=weeks[1])] == weeks[1:]
    assert [item.dimension for item in metric_snapshots.for_week(conn, weeks[2])] == ["all", "api-fuzz"]


def test_records_reject_missing_required_columns(conn):
    with pytest.raises(sqlite3.IntegrityError):
        suggestions.save(conn, SuggestionRecord("LS-0023", SuggestionKind.CONTROL, None, SuggestionStatus.PENDING, T0))
