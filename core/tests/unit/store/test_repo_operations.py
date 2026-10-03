from dataclasses import replace
from datetime import date, timedelta

import pytest

from tightrein.domain.enums import AgentSessionStatus, OperationExecutor, OperationKind, OperationStatus, Stage
from tightrein.store.repos import (agent_sessions, budget_usage, incidental_sources, pending_claims, pending_operations,
                                    probe_states, source_cursors)
from tightrein.store.repos.agent_sessions import AgentSession
from tightrein.store.repos.incidental_sources import IncidentalSource
from tightrein.store.repos.pending_operations import PendingOperationRecord
from tightrein.store.repos.pending_claims import PendingClaim
from tightrein.store.repos.probe_states import ProbeState
from tightrein.store.repos.source_cursors import SourceCursor

from store_samples import T0


def operation(operation_id="OP-0015", **changes):
    values = dict(
        id=operation_id, stage=Stage.RELEASE, subject_id="0007", kind=OperationKind.PUSH,
        executor=OperationExecutor.VCS, impact="推送到远程分支 fix/0007", reversible=False,
        idempotency_key="push:fix/0007", confirmations_required=1, status=OperationStatus.PENDING, created_at=T0,
        commands=[{"argv": ["git", "push", "origin", "fix/0007"], "cwd": "worktrees/fix-0007", "note": "推送"}],
        description={"repository": "sample", "branch": "fix/0007", "files": ["a.cs", "b.cs"], "remote": True},
        preconditions={"head": "abc1234"},
    )
    values.update(changes)
    return PendingOperationRecord(**values)


def test_pending_operation_round_trip(conn):
    pending_operations.save(conn, operation())
    assert pending_operations.get(conn, "OP-0015") == operation()
    executed = operation(status=OperationStatus.EXECUTED, confirmations_given=1, decided_at=T0,
                         executed_at=T0 + timedelta(minutes=1), result={"stdout": "ok"})
    pending_operations.save(conn, executed)
    assert pending_operations.get(conn, "OP-0015") == executed
    assert pending_operations.get(conn, "OP-0404") is None


def test_find_pending_operations(conn):
    pending_operations.save(conn, operation())
    pending_operations.save(conn, operation("OP-0016", kind=OperationKind.CLEANUP, confirmations_required=2,
                                            confirmations_given=1, created_at=T0 + timedelta(minutes=1)))
    pending_operations.save(conn, operation("OP-0017", subject_id="0008", status=OperationStatus.REJECTED))
    assert [item.id for item in pending_operations.find(conn, subject_id="0007")] == ["OP-0015", "OP-0016"]
    assert [item.id for item in pending_operations.find(conn, status=OperationStatus.PENDING)] == [
        "OP-0015", "OP-0016"]


def test_confirmation_counts_are_checked():
    with pytest.raises(ValueError):
        operation(confirmations_required=3)
    with pytest.raises(ValueError):
        operation(confirmations_given=2)


def test_agent_sessions(conn):
    first = AgentSession(Stage.FIX, "fix-executor", "0007", T0, "claude", "worktrees/fix-0007",
                         AgentSessionStatus.CLOSED, "session-1", T0 + timedelta(minutes=30))
    second = AgentSession(Stage.FIX, "fix-executor", "0007", T0 + timedelta(hours=1), "claude",
                          "worktrees/fix-0007", AgentSessionStatus.OPEN)
    agent_sessions.save(conn, second)
    agent_sessions.save(conn, first)
    assert agent_sessions.latest(conn, Stage.FIX, "fix-executor", "0007") == second
    assert agent_sessions.find(conn, status=AgentSessionStatus.CLOSED) == [first]
    assert agent_sessions.latest(conn, Stage.FIX, "fix-scout", "0007") is None
    agent_sessions.save(conn, replace(second, status=AgentSessionStatus.CLOSED, session_id="session-2"))
    assert len(agent_sessions.find(conn, subject_id="0007")) == 2


def test_source_cursors(conn):
    cursor = SourceCursor("platform-errors:error-tracking", {"until": "2026-09-29T02:15:00Z"}, T0)
    source_cursors.save(conn, cursor)
    moved = replace(cursor, cursor={"until": "2026-09-29T03:15:00Z"}, parse_state={"stream": "2026-09-29T02:15:03"},
                    updated_at=T0 + timedelta(minutes=5))
    source_cursors.save(conn, moved)
    assert source_cursors.get(conn, "platform-errors:error-tracking") == moved
    assert source_cursors.TABLE.find(conn) == [moved]
    assert source_cursors.get(conn, "access-log") is None


def test_probe_states_and_pending_claims(conn):
    state = ProbeState("daily-import", T0, {"lastBatch": "2026-09-28"})
    probe_states.save(conn, state)
    probe_states.save(conn, replace(state, last_run_at=T0 + timedelta(days=1)))
    assert probe_states.get(conn, "daily-import").last_run_at == T0 + timedelta(days=1)
    low = PendingClaim("PC-0000000001", {"file": "a.py", "line": 3}, "low", pending_claims.LOW, "R-1", T0)
    over = replace(low, id="PC-0000000002", severity="high", reason=pending_claims.OVER_LIMIT)
    for item in (low, over):
        pending_claims.save(conn, item)
    assert pending_claims.find(conn, reason=pending_claims.LOW) == [low]
    pending_claims.save(conn, replace(over, state=pending_claims.VERIFIED))
    assert pending_claims.find(conn) == [low]


def test_incidental_sources(conn):
    source = IncidentalSource("data/runs/R-20260929-021503-fix/handoff/fix-0007.json", "5" * 64, T0, 2)
    incidental_sources.save(conn, source)
    assert incidental_sources.get(conn, source.source_path) == source
    assert incidental_sources.TABLE.find(conn) == [source]


def test_budget_usage_accumulates_per_stage_and_day(conn, clock):
    day = date(2026, 10, 5)
    budget_usage.add(conn, Stage.TRIAGE, day, 0.25, 1000, 200, False, clock)
    clock.advance(timedelta(minutes=5))
    total = budget_usage.add(conn, Stage.TRIAGE, day, 0.5, 3000, 100, True, clock)
    budget_usage.add(conn, Stage.TRIAGE, day, 0.25, 0, 0, False, clock)
    budget_usage.add(conn, Stage.FIX, day - timedelta(days=1), 1.0, 10, 10, False, clock)
    assert (total.cost_usd, total.input_tokens, total.output_tokens, total.estimated) == (0.75, 4000, 300, True)
    stored = budget_usage.get(conn, Stage.TRIAGE, day)
    assert (stored.cost_usd, stored.estimated, stored.updated_at) == (1.0, True, clock.now())
    assert [item.stage for item in budget_usage.find(conn)] == [Stage.FIX, Stage.TRIAGE]
    assert [item.stage for item in budget_usage.find(conn, since=day)] == [Stage.TRIAGE]
    assert budget_usage.find(conn, stage=Stage.VERIFY) == []
