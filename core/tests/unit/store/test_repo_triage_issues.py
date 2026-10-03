from datetime import timedelta

import pytest

from tightrein.contracts.validate import SchemaValidationError
from tightrein.domain.enums import CloseReason, Disposition, IssueEvent, IssueStatus, Stage, TriageOutcome, Verdict
from tightrein.domain.issue import Hold
from tightrein.domain.triage import TriageResult
from tightrein.store.repos import issue_events, issues, triage
from tightrein.store.repos.issue_events import USER_EDITED, IssueEventRecord
from tightrein.store.repos.issues import IssueRecord
from tightrein.store.repos.triage import TriageRecord

from store_samples import T0, closed_issue, issue, triage_result

RUN = "R-20260929-021503-triage"


def record(result, created_at=T0, run_id=RUN):
    return TriageRecord(result, run_id, created_at)


def test_triage_round_trip(conn):
    full = record(triage_result())
    minimal = record(TriageResult("P-0002", 1, Verdict.REFUTED, Disposition.FALSE_POSITIVE, "上游已校验", "abc1234"))
    triage.save(conn, full)
    triage.save(conn, minimal)
    assert triage.get(conn, "P-0001", 1) == full
    assert triage.get(conn, "P-0002", 1) == minimal
    assert triage.get(conn, "P-0001", 2) is None


def test_attempts_latest_and_next_attempt(conn):
    assert triage.next_attempt(conn, "P-0001") == 1
    triage.save(conn, record(triage_result(attempt=2), T0 + timedelta(hours=1)))
    triage.save(conn, record(triage_result(attempt=1)))
    assert [item.result.attempt for item in triage.for_problem(conn, "P-0001")] == [1, 2]
    assert triage.latest(conn, "P-0001").result.attempt == 2
    assert triage.latest(conn, "P-0404") is None
    assert triage.next_attempt(conn, "P-0001") == 3


def test_find_triage_by_disposition_and_run(conn):
    triage.save(conn, record(triage_result("P-0003"), T0 + timedelta(minutes=1)))
    triage.save(conn, record(triage_result("P-0001", disposition=Disposition.DEFERRED), run_id="R-20260930-000000-triage"))
    triage.save(conn, record(triage_result("P-0002")))
    found = triage.find(conn, disposition=Disposition.CREATE_ISSUE)
    assert [item.result.problem_id for item in found] == ["P-0002", "P-0003"]
    assert [item.result.problem_id for item in triage.find(conn, run_id="R-20260930-000000-triage")] == ["P-0001"]


def test_set_outcome_fills_result_and_time(conn):
    triage.save(conn, record(triage_result()))
    updated = triage.set_outcome(conn, "P-0001", 1, TriageOutcome.CORRECT, T0 + timedelta(days=3))
    assert triage.get(conn, "P-0001", 1) == updated
    assert (updated.result.outcome, updated.outcome_at) == (TriageOutcome.CORRECT, T0 + timedelta(days=3))
    with pytest.raises(LookupError):
        triage.set_outcome(conn, "P-0001", 2, TriageOutcome.CORRECT, T0)


def index(item, path=None):
    return IssueRecord(item, path or f"issues/{item.id}-{item.slug}.md", "0" * 64)


def test_issue_round_trip(conn):
    issues.save(conn, index(issue()))
    issues.save(conn, index(closed_issue()))
    assert issues.get(conn, "0007") == index(issue())
    assert issues.get(conn, "0008") == index(closed_issue())
    assert issues.get(conn, "0404") is None


def test_invalid_hold_is_rejected(conn):
    with pytest.raises(SchemaValidationError, match=r"\$\.reason"):
        issues.save(conn, index(issue(hold=Hold("", Stage.FIX, T0))))


def test_find_issues_by_status_and_problem(conn):
    issues.save(conn, index(issue("0010", problems=("P-0003",))))
    issues.save(conn, index(issue()))
    issues.save(conn, index(closed_issue()))
    assert [item.issue.id for item in issues.find(conn)] == ["0007", "0008", "0010"]
    assert [item.issue.id for item in issues.find(conn, status=IssueStatus.NEEDS_DECISION)] == ["0007", "0010"]
    assert [item.issue.id for item in issues.by_problem(conn, "P-0003")] == ["0007", "0008", "0010"]
    assert [item.issue.id for item in issues.by_problem(conn, "P-0001")] == ["0007", "0008"]
    assert issues.by_problem(conn, "P-0404") == []
    issues.remove(conn, "0010")
    assert issues.get(conn, "0010") is None


def test_issue_events_append_in_time_order(conn):
    later = issue_events.append(conn, IssueEventRecord("0007", T0 + timedelta(hours=1), USER_EDITED, "user"))
    earlier = issue_events.append(conn, IssueEventRecord(
        "0007", T0, IssueEvent.USER_CLOSED.value, "user", IssueStatus.TODO, IssueStatus.CANCELLED,
        CloseReason.WONT_FIX, "成本过高",
    ))
    stored = issue_events.for_issue(conn, "0007")
    assert [item.id for item in stored] == [earlier, later]
    assert stored[0].close_reason is CloseReason.WONT_FIX
    assert issue_events.for_issue(conn, "0008") == []
    with pytest.raises(ValueError):
        IssueEventRecord("0007", T0, "renamed", "user")
