import json
from datetime import timedelta

from tightrein.domain.enums import ProblemEvent, ProblemStatus, RunStatus
from tightrein.domain.run import EnvironmentDetail, HealthCheck
from tightrein.domain.state_machine import InvalidTransition
from tightrein.pipeline.aggregate.changeset import TransitionRejected
from tightrein.pipeline.aggregate.manual import ProblemCommands
from tightrein.pipeline.aggregate.service import AggregateRequest
from tightrein.pipeline.aggregate.steps import status
from tightrein.store.repos import problems, runs
from aggregate_world import collect_run, error_on, make_service, save_run
from pipeline_world import NOW, make_world

SCHEMA_CHECK = "response_schema_conformance"


def state(conn):
    rows = []
    for problem in problems.find(conn):
        rows.append((problem.id, problem.fingerprint, problem.status, problem.occurrences, problem.issue_id,
                     problem.merged_into, problem.ignore_until is not None,
                     tuple(problems.signal_ids(conn, problem.id))))
    return rows


def aggregate_at(world, hours):
    world.clock.advance(NOW + timedelta(hours=hours) - world.clock.now())
    return make_service(world).run(AggregateRequest())


def build_history(world):
    first = collect_run(1)
    save_run(world.conn, first, [error_on(1, first), error_on(2, first, "/api/Item/1"),
                                 error_on(3, first, check=SCHEMA_CHECK)])
    aggregate_at(world, 1.5)
    commands = ProblemCommands(world.layout, world.config, world.conn, world.clock, world.events)
    world.clock.advance(timedelta(minutes=10))
    commands.ignore("P-0002", "等下个版本")
    commands.merge("P-0001", "P-0003")
    blocked = collect_run(2, status=RunStatus.BLOCKED,
                          environment_detail=EnvironmentDetail(health=HealthCheck(None)))
    save_run(world.conn, blocked)
    second = collect_run(3)
    save_run(world.conn, second, [error_on(4, second), error_on(5, second, check=SCHEMA_CHECK),
                                  error_on(6, second, "/api/User/1")])
    aggregate_at(world, 3.5)


def test_rebuild_matches_incremental_aggregation_and_keeps_every_id(tmp_path):
    world = make_world(tmp_path)
    build_history(world)
    before = state(world.conn)
    assert [row[2] for row in before] == [ProblemStatus.NEW, ProblemStatus.IGNORED, ProblemStatus.NEW,
                                          ProblemStatus.NEW]
    assert before[2][5] == "P-0001"
    world.clock.advance(timedelta(hours=1))
    result = make_service(world).run(AggregateRequest(rebuild=True))
    assert result.run.status is RunStatus.OK
    assert state(world.conn) == before
    document = json.loads(result.handoffs[0].read_text(encoding="utf-8"))
    rebuild = document["outputs"]["rebuild"]
    assert [item["problemId"] for item in rebuild["inherited"]] == ["P-0001", "P-0002", "P-0003", "P-0004"]
    assert rebuild["split"] == []
    assert rebuild["merged"] == [{"problemId": "P-0001", "previousIds": ["P-0001", "P-0003"]}]
    assert list(world.layout.archive_dir().glob("tightrein-*.db"))


def test_a_failed_rebuild_rolls_back(tmp_path, monkeypatch):
    world = make_world(tmp_path)
    build_history(world)
    before = state(world.conn)

    def broken(*args, **kwargs):
        raise TransitionRejected("P-0001", InvalidTransition(ProblemStatus.NEW, ProblemEvent.REPRODUCED))

    monkeypatch.setattr(status, "apply", broken)
    result = make_service(world).run(AggregateRequest(rebuild=True))
    assert result.exit_code == 1 and state(world.conn) == before
    assert all(run.aggregated_at is not None for run in runs.find(world.conn) if run.probe is not None)


def test_rebuild_dry_run_lists_every_collect_run(tmp_path):
    world = make_world(tmp_path)
    build_history(world)
    plan = make_service(world).run(AggregateRequest(rebuild=True, dry_run=True)).plan
    assert [item.signals for item in plan.runs] == [3, 0, 3]
