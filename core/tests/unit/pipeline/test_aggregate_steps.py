from dataclasses import replace
from datetime import date, timedelta

from tightrein.domain.enums import Probe, ProblemStatus, RunStage, RunStatus, SignalAggregateState
from tightrein.domain.fingerprint import CURRENT_VERSION, fingerprint
from tightrein.domain.normalize import Rule
from tightrein.domain.run import Coverage, Endpoint, EnvironmentDetail, HealthCheck, Run
from tightrein.domain.suppression import SuppressionRule
from tightrein.pipeline.aggregate.changeset import ChangeSet
from tightrein.pipeline.aggregate.steps import group, normalize, suppress
from tightrein.store import sequences
from pipeline_world import NOW, RELEASE, RUN_ID, make_signal, make_world
from store_problem import save_problem

TITLE_LENGTH = 120

ENDPOINTS = tuple(Endpoint("GET", f"/api/Item{number}/{{id}}", "Admin") for number in range(4))


def api_run(**changes):
    values = dict(id=RUN_ID, stage=RunStage.COLLECT, started_at=NOW, status=RunStatus.OK, probe=Probe.API_FUZZ,
                  ended_at=NOW + timedelta(minutes=5), target_commit=RELEASE, coverage=Coverage(endpoints=ENDPOINTS),
                  environment_detail=EnvironmentDetail(health=HealthCheck(200, 5)))
    values.update(changes)
    return Run(**values)


def changeset(world):
    return ChangeSet.start(world.conn, "R-20261005-030000-aggregate", NOW, 30)


def test_register_counts_the_run_and_marks_it_aggregated(tmp_path):
    world = make_world(tmp_path)
    cs = changeset(world)
    cs.register(api_run())
    assert cs.current.to_dict() == {"runId": RUN_ID, "probe": "api-fuzz", "grouped": 0, "suppressed": 0}
    assert cs.runs[RUN_ID].aggregated_at == NOW


def test_normalize_and_suppress(tmp_path):
    world = make_world(tmp_path)
    cs = changeset(world)
    cs.register(api_run())
    signals = [make_signal(1, message="批次 lot-77 不存在"), make_signal(2, message="订单 12345 不存在")]
    normalized = normalize.apply(cs, signals, (Rule(r"lot-\d+", "<lot>"),))
    assert [signal.normalized_message for signal in normalized] == ["批次 <lot> 不存在", "订单 <num> 不存在"]
    rule = SuppressionRule("已知", date(2026, 10, 1), date(2026, 10, 30), probe=Probe.API_FUZZ, message_pattern="批次")
    expired = replace(rule, message_pattern="订单", expires_on=date(2026, 10, 4))
    remaining = suppress.apply(cs, normalized, [rule, expired], NOW)
    assert [signal.id for signal in remaining] == [make_signal(2).id]
    suppressed = cs.signals[make_signal(1).id]
    assert suppressed.suppressed and suppressed.aggregate_state is SignalAggregateState.DONE
    assert cs.current.suppressed == 1


def test_grouping_creates_and_appends_by_fingerprint(tmp_path):
    world = make_world(tmp_path)
    cs = changeset(world)
    cs.register(api_run())
    signals = normalize.apply(cs, [make_signal(1), make_signal(2, location="GET /api/Order/43"),
                                   make_signal(3, location="POST /api/Order")], ())
    group.apply(cs, world.conn, signals, TITLE_LENGTH)
    assert cs.created == ["P-0001", "P-0002"]
    assert cs.problems["P-0001"].occurrences == 2 and cs.problems["P-0001"].status is ProblemStatus.PENDING
    assert cs.occurred == {"P-0001": [signals[0].id, signals[1].id], "P-0002": [signals[2].id]}
    assert cs.current.grouped == 3
    assert cs.signals[signals[0].id].fingerprint == fingerprint(signals[0], CURRENT_VERSION)


def test_existing_problems_and_regression_signals(tmp_path):
    world = make_world(tmp_path)
    signal = normalize.apply(changeset(world), [make_signal(1)], ())[0]
    save_problem(world.conn, "P-0003", fingerprint(signal, CURRENT_VERSION), occurrences=4)
    save_problem(world.conn, "P-0004", "c" * 16, issue_id="0007", status=ProblemStatus.RESOLVED)
    cs = changeset(world)
    cs.register(api_run())
    regression = make_signal(2, check="regression", context={
        "issue": "0007", "checkId": "api-1", "targetFingerprints": ["f" * 16, "c" * 16], "detail": "500"})
    orphan = make_signal(3, check="regression", context={
        "issue": "0008", "checkId": "api-1", "targetFingerprints": ["e" * 16], "detail": "500"})
    group.apply(cs, world.conn, normalize.apply(cs, [signal, regression, orphan], ()), TITLE_LENGTH)
    assert cs.created == [] and cs.problems["P-0003"].occurrences == 5
    assert cs.regression_hits == {"P-0004"} and cs.occurred["P-0004"] == [regression.id]
    assert cs.signals[regression.id].fingerprint is None
    assert cs.notes == [f"回归信号 {orphan.id} 的目标指纹没有对应的问题"]
    assert sequences.current(world.conn, sequences.PROBLEM) == 4
