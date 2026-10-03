from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from tightrein.domain.enums import (
    CloseReason,
    IssueStatus,
    ProblemEvent,
    ProblemStatus,
    SignalAggregateState,
)
from tightrein.domain.problem import IgnoreCondition, ProblemContext
from tightrein.pipeline.aggregate.changeset import ChangeSet, context_from_dict, context_to_dict
from tightrein.pipeline.aggregate.steps import apply
from tightrein.store import sequences
from tightrein.store.files import issue_files, suppressions
from tightrein.store.repos import issue_events, issues, problem_events, problems, runs, signals
from pipeline_world import NOW, make_signal, make_world, write_issue_file
from store_problem import make_problem, save_problem

AGGREGATE_RUN = "R-20261005-030000-aggregate"


def changeset(world):
    return ChangeSet.start(world.conn, AGGREGATE_RUN, NOW, 30)


def test_effects_are_applied_and_events_recorded(tmp_path):
    world = make_world(tmp_path)
    cs = changeset(world)
    condition = IgnoreCondition(until=datetime(2026, 11, 1, tzinfo=timezone.utc))
    ignored = cs.transition(make_problem(), ProblemEvent.USER_IGNORED, ProblemContext(ignore_until=condition),
                            operation="user_action", reason="等下个版本")
    assert (ignored.status, ignored.ignore_until) == (ProblemStatus.IGNORED, condition)
    reopened = cs.transition(ignored, ProblemEvent.USER_REOPENED, operation="user_action")
    assert reopened.ignore_until is None and reopened.status is ProblemStatus.NEW
    cs.transition(reopened, ProblemEvent.USER_FALSE_POSITIVE, operation="user_action", reason="测试数据",
                  suppression_expires=date(2026, 12, 1))
    [rule] = cs.suppressions
    assert (rule.fingerprint, rule.reason, rule.expires_on) == ("a1b2c3d4e5f60718", "测试数据", date(2026, 12, 1))
    resolved = cs.transition(make_problem("P-0002", "b" * 16), ProblemEvent.COVERED_RUN_WITHOUT_OCCURRENCE,
                             ProblemContext(ready_to_resolve=True, release="c" * 40))
    assert (resolved.status, resolved.resolved_release) == (ProblemStatus.RESOLVED, "c" * 40)
    assert [(change.problem_id, change.to_status) for change in cs.changes] == [
        ("P-0001", ProblemStatus.IGNORED), ("P-0001", ProblemStatus.NEW), ("P-0001", ProblemStatus.IGNORED),
        ("P-0002", ProblemStatus.RESOLVED)]
    assert context_from_dict(cs.events[0].detail["context"]) == ProblemContext(ignore_until=condition)
    assert cs.events[0].operation == "user_action" and cs.events[0].run_id == AGGREGATE_RUN


def test_context_round_trip():
    context = ProblemContext(close_reason=CloseReason.WONT_FIX, regressed=True, issue_id="0007", merge_target="P-0003",
                             ignore_until=IgnoreCondition(occurrences=2), release="c1")
    assert context_from_dict(context_to_dict(context)) == context
    assert context_to_dict(ProblemContext()) == {}


def test_allocation_continues_the_sequence(tmp_path):
    world = make_world(tmp_path)
    sequences.ensure_at_least(world.conn, sequences.PROBLEM, 4)
    cs = changeset(world)
    assert [cs.allocate(), cs.allocate(), cs.allocate("P-0010"), cs.allocate()] == [
        "P-0005", "P-0006", "P-0010", "P-0011"]


def test_commit_writes_the_whole_changeset(tmp_path):
    world = make_world(tmp_path)
    save_problem(world.conn, "P-0001", "a" * 16)
    cs = changeset(world)
    signal = replace(make_signal(1), fingerprint="b" * 16, aggregate_state=SignalAggregateState.DONE)
    cs.put_signal(signal)
    created = make_problem(cs.allocate(), "b" * 16, status=ProblemStatus.PENDING)
    cs.put_problem(created, created=True)
    cs.record_occurrence(created.id, signal.id)
    cs.transition(created, ProblemEvent.REPRODUCED)
    apply.commit(world.conn, cs, world.clock, world.layout, side_effects=True)
    assert problems.get(world.conn, "P-0002").status is ProblemStatus.NEW
    assert problems.signal_ids(world.conn, "P-0002") == [signal.id]
    assert signals.get(world.conn, signal.id).aggregate_state is SignalAggregateState.DONE
    assert [event.event for event in problem_events.for_problem(world.conn, "P-0002")] == [ProblemEvent.REPRODUCED]
    assert sequences.current(world.conn, sequences.PROBLEM) == 2


def regressed_changeset(world):
    write_issue_file(world)
    problem = save_problem(world.conn, status=ProblemStatus.RESOLVED, issue_id="0007", resolved_release="c1")
    cs = changeset(world)
    cs.occurred["P-0001"] = ["S-01J9Z300000000000000000001"]
    cs.transition(problem, ProblemEvent.SEEN_AGAIN, ProblemContext(regressed=True, issue_id="0007"))
    return cs


def test_regression_reopens_the_issue_and_appends_history(tmp_path):
    world = make_world(tmp_path)
    cs = regressed_changeset(world)
    assert apply.commit(world.conn, cs, world.clock, world.layout, side_effects=True) == ["0007"]
    record = issues.get(world.conn, "0007")
    assert (record.issue.status, record.issue.close_reason) == (IssueStatus.TODO, None)
    document = issue_files.read(world.root / record.path)
    assert "## 历史" in document.body and "关联问题 P-0001 回归" in document.body
    assert issue_events.for_issue(world.conn, "0007")[0].event == "problem-regressed"
    assert problems.get(world.conn, "P-0001").status is ProblemStatus.REGRESSED


def test_failure_rolls_everything_back_and_restores_files(tmp_path, monkeypatch):
    world = make_world(tmp_path)
    cs = regressed_changeset(world)
    cs.transition(make_problem("P-0002", "b" * 16), ProblemEvent.USER_FALSE_POSITIVE, operation="user_action")
    path = world.root / issues.get(world.conn, "0007").path
    before = path.read_text(encoding="utf-8")

    def broken(*args, **kwargs):
        raise OSError("磁盘已满")

    monkeypatch.setattr(suppressions, "add_rule", broken)
    with pytest.raises(OSError):
        apply.commit(world.conn, cs, world.clock, world.layout, side_effects=True)
    assert path.read_text(encoding="utf-8") == before
    assert issues.get(world.conn, "0007").issue.status is IssueStatus.DONE
    assert problems.get(world.conn, "P-0001").status is ProblemStatus.RESOLVED
    assert problems.get(world.conn, "P-0002") is None
    assert problem_events.for_problem(world.conn, "P-0001") == []
    assert runs.find(world.conn) == []


def test_without_side_effects_files_stay_unchanged(tmp_path):
    world = make_world(tmp_path)
    cs = regressed_changeset(world)
    assert apply.commit(world.conn, cs, world.clock, world.layout, side_effects=False) == []
    assert issues.get(world.conn, "0007").issue.status is IssueStatus.DONE
    assert not world.layout.suppressions().exists()


def test_history_is_appended_to_an_existing_section():
    body = "## 描述\n\n正文\n\n## 历史\n\n- 第一行\n\n## 附录\n\n附录\n"
    assert apply.append_history(body, "- 第二行") == \
        "## 描述\n\n正文\n\n## 历史\n\n- 第一行\n- 第二行\n\n## 附录\n\n附录\n"
