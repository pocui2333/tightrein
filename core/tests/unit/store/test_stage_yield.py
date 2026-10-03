from datetime import datetime, timedelta, timezone

import pytest

from tightrein.domain.clock import FixedClock
from tightrein.domain.enums import RunnerStatus, Stage, YieldOutcome
from tightrein.store.migrations.runner import open_database
from tightrein.store.repos import stage_yield
from tightrein.store.repos.stage_yield import StageYieldRecord

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    connection = open_database(tmp_path / "tightrein.db", FixedClock(NOW))
    yield connection
    connection.close()


def record(role="claim-verifier", at=NOW, stage=Stage.TRIAGE, **changes):
    values = dict(run_id="R-20261005-030000-triage", stage=stage, role=role, subject_id="P-0001", attempt=1,
                  runner_status=RunnerStatus.OK, created_at=at, input_tokens=100, output_tokens=20, cost_usd=0.01)
    values.update(changes)
    return StageYieldRecord(**values)


def test_appended_records_are_pending_and_found_in_order(conn):
    second = stage_yield.append(conn, record("refuter", NOW + timedelta(minutes=1)))
    first = stage_yield.append(conn, record())
    stage_yield.append(conn, record("fix-scout", NOW - timedelta(days=8), Stage.FIX, subject_id="0007"))
    found = stage_yield.find(conn, since=NOW - timedelta(days=7))
    assert [(row.id, row.role, row.outcome) for row in found] == [
        (first, "claim-verifier", YieldOutcome.PENDING), (second, "refuter", YieldOutcome.PENDING)]
    assert [row.role for row in stage_yield.find(conn, stage=Stage.FIX)] == ["fix-scout"]


def test_decide_fills_the_outcome(conn):
    record_id = stage_yield.append(conn, record())
    stage_yield.decide(conn, record_id, YieldOutcome.USEFUL, "实际结果为判对", NOW)
    (found,) = stage_yield.find(conn, outcome=YieldOutcome.USEFUL)
    assert (found.outcome_reason, found.decided_at) == ("实际结果为判对", NOW)
    assert stage_yield.find(conn, outcome=YieldOutcome.PENDING) == []


def test_a_record_with_an_id_is_rejected(conn):
    with pytest.raises(ValueError):
        stage_yield.append(conn, record(id=3))
