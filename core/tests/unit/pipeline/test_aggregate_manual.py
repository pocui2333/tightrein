from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from tightrein.domain.enums import ProblemEvent, ProblemStatus
from tightrein.domain.fingerprint import CURRENT_VERSION, fingerprint
from tightrein.domain.normalize import normalize
from tightrein.pipeline.aggregate.manual import ProblemCommands
from tightrein.pipeline.aggregate.service import AggregateRequest
from tightrein.store.files import suppressions
from tightrein.store.repos import problem_events, problems
from tightrein.store.repos.problems import MergeRejected
from aggregate_world import collect_run, error_on, make_service, save_run
from pipeline_world import make_world
from store_problem import save_problem


def commands(world):
    return ProblemCommands(world.layout, world.config, world.conn, world.clock, world.events)


def test_ignore_with_conditions_and_reopen(tmp_path):
    world = make_world(tmp_path)
    save_problem(world.conn, occurrences=4)
    until = datetime(2026, 11, 1, tzinfo=timezone.utc)
    ignored = commands(world).ignore("P-0001", "等下个版本", until=until, occurrences=3)
    condition = ignored.ignore_until
    assert ignored.status is ProblemStatus.IGNORED
    assert (condition.until, condition.occurrences, condition.baseline_occurrences) == (until, 3, 4)
    reopened = commands(world).reopen("P-0001", "已确认要修")
    assert (reopened.status, reopened.ignore_until, reopened.clean_covered_runs) == (ProblemStatus.NEW, None, 0)
    events = problem_events.for_problem(world.conn, "P-0001")
    assert [(event.event, event.operation, event.reason) for event in events] == [
        (ProblemEvent.USER_IGNORED, "user_action", "等下个版本"), (ProblemEvent.USER_REOPENED, "user_action", "已确认要修")]


def test_false_positive_writes_a_suppression_rule(tmp_path):
    world = make_world(tmp_path)
    save_problem(world.conn)
    commands(world).false_positive("P-0001", "测试数据导致")
    [rule] = suppressions.read(world.layout.suppressions())
    assert (rule.fingerprint, rule.reason, rule.expires_on) == ("a1b2c3d4e5f60718", "测试数据导致", date(2026, 11, 4))
    commands(world).reopen("P-0001")
    commands(world).false_positive("P-0001", "再次确认", expires=date(2026, 12, 31))
    [rule] = suppressions.read(world.layout.suppressions())
    assert rule.expires_on == date(2026, 12, 31)
    assert problems.get(world.conn, "P-0001").status is ProblemStatus.IGNORED


def fingerprint_of(signal):
    return fingerprint(replace(signal, normalized_message=normalize(signal.message, ())), CURRENT_VERSION)


def test_merge_routes_new_signals_of_the_source_to_the_target(tmp_path):
    world = make_world(tmp_path)
    run = collect_run(1)
    source_signal = error_on(1, run, "/api/Item/3")
    save_problem(world.conn, "P-0001", "a" * 16)
    save_problem(world.conn, "P-0002", fingerprint_of(source_signal))
    target = commands(world).merge("P-0001", "P-0002")
    assert problems.get(world.conn, "P-0002").merged_into == "P-0001"
    assert problems.aliases(world.conn, "P-0001") == [fingerprint_of(source_signal)]
    assert problem_events.for_problem(world.conn, "P-0002")[-1].event is ProblemEvent.MERGED
    save_run(world.conn, run, [source_signal])
    make_service(world).run(AggregateRequest())
    assert problems.signal_ids(world.conn, "P-0001") == [source_signal.id]
    assert problems.get(world.conn, "P-0001").occurrences == target.occurrences + 1


def test_rejected_operations(tmp_path):
    world = make_world(tmp_path)
    save_problem(world.conn, "P-0001", "a" * 16)
    save_problem(world.conn, "P-0002", "b" * 16, issue_id="0007")
    with pytest.raises(LookupError):
        commands(world).ignore("P-0009", "不存在")
    with pytest.raises(MergeRejected):
        commands(world).merge("P-0001", "P-0002")
    assert problem_events.for_problem(world.conn, "P-0002") == []
