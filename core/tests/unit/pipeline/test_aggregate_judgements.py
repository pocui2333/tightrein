from dataclasses import replace
from datetime import datetime, timedelta, timezone

from tightrein.domain.enums import Probe, ProblemEvent, ProblemStatus, RunStage, RunStatus
from tightrein.domain.fingerprint import CURRENT_VERSION, fingerprint
from tightrein.domain.problem import IgnoreCondition, ProblemScope
from tightrein.domain.run import Coverage, Endpoint, EnvironmentDetail, HealthCheck, Run
from tightrein.pipeline.aggregate import commit_facts
from tightrein.pipeline.aggregate.changeset import ChangeSet
from tightrein.pipeline.aggregate.steps import apply, group, normalize, reproduce, status
from tightrein.store.repos import problems, runs, signals
from tightrein.vcs.errors import RefNotFound
from pipeline_world import NOW, RELEASE, make_signal, make_world
from store_problem import save_problem

TITLE_LENGTH = 120

NEWER = "d" * 40
ORDER = Endpoint("GET", "/api/Order/{id}", "Admin")
ENDPOINTS = (ORDER, *(Endpoint("GET", f"/api/Item{number}", "Admin") for number in range(3)))


def collect_run(number, probe=Probe.API_FUZZ, commit=RELEASE, coverage=None, **changes):
    started = NOW + timedelta(hours=number)
    values = dict(id=f"R-{started:%Y%m%d-%H%M%S}-collect-{probe.value}", stage=RunStage.COLLECT, started_at=started,
                  status=RunStatus.OK, probe=probe, ended_at=started + timedelta(minutes=5), target_commit=commit,
                  coverage=coverage or Coverage(endpoints=ENDPOINTS),
                  environment_detail=EnvironmentDetail(health=HealthCheck(200)))
    values.update(changes)
    return Run(**values)


def grouped(world, run, found):
    """登记运行并执行规范化与指纹归并，返回 ChangeSet。"""
    cs = ChangeSet.start(world.conn, "R-20261005-030000-aggregate", NOW, 30)
    cs.register(run)
    group.apply(cs, world.conn, normalize.apply(cs, list(found), ()), TITLE_LENGTH)
    return cs


def fingerprint_of(world, signal):
    normalized = normalize.apply(ChangeSet.start(world.conn, None, NOW, 30), [signal], ())[0]
    return fingerprint(normalized, CURRENT_VERSION)


def reproduce_step(cs, world, replayer=None):
    reproduce.apply(cs, world.conn, replayer=replayer, attempts=2)


class Ancestry:
    def __init__(self, known):
        self.known = known
        self.asked = []

    def is_ancestor(self, repo, commit, of):
        self.asked.append((commit, of))
        if (commit, of) not in self.known:
            raise RefNotFound(f"{commit} 不存在")
        return self.known[(commit, of)]


def test_commit_facts_treat_missing_commits_as_unknown(tmp_path):
    reader = Ancestry({(RELEASE, NEWER): True})
    pairs = [(RELEASE, NEWER), (RELEASE, NEWER), ("x" * 40, NEWER), (NEWER, NEWER)]
    facts = commit_facts.query(reader, tmp_path, pairs)
    assert facts.is_newer(NEWER, RELEASE) is True
    assert facts.is_newer(NEWER, "x" * 40) is None
    assert len(reader.asked) == 2


def test_new_problems_are_confirmed_by_strategy(tmp_path):
    world = make_world(tmp_path)
    run = collect_run(1)
    found = [make_signal(1, run_id=run.id), make_signal(2, run_id=run.id, location="POST /api/Order"),
             make_signal(3, run_id=run.id, location="PUT /api/Order")]
    cs = grouped(world, run, found)
    answers = iter([[True, False], [False, False], [None, False]])
    reproduce_step(cs, world, lambda signal, attempts: next(answers))
    assert [cs.problems[pid].status for pid in cs.created] == [
        ProblemStatus.NEW, ProblemStatus.PENDING, ProblemStatus.PENDING]
    assert [cs.problems[pid].intermittent for pid in cs.created] == [False, True, False]
    assert cs.reproduction["P-0001"].to_dict() == {"strategy": "replay", "attempts": 2, "reproduced": True}
    log = collect_run(1, Probe.PLATFORM_ERRORS, coverage=Coverage(sources=("log-platform",)))
    cs = grouped(world, log, [make_signal(4, run_id=log.id, probe=Probe.PLATFORM_ERRORS, check="error",
                                          location="OrderService.Get", context={"category": "Order"})])
    reproduce_step(cs, world)
    assert cs.problems[cs.created[0]].status is ProblemStatus.NEW


def test_without_replayer_replay_problems_stay_pending(tmp_path):
    world = make_world(tmp_path)
    run = collect_run(1)
    cs = grouped(world, run, [make_signal(1, run_id=run.id)])
    reproduce_step(cs, world)
    assert cs.problems["P-0001"].status is ProblemStatus.PENDING and cs.changes == []


def test_unreplayable_api_fuzz_checks_are_valid_on_first_sight(tmp_path):
    world = make_world(tmp_path)
    run = collect_run(1)
    cs = grouped(world, run, [make_signal(1, run_id=run.id, check="unsupported_method", location="TRACE /api/Order")])
    reproduce_step(cs, world, lambda signal, attempts: [None] * attempts)
    assert cs.problems["P-0001"].status is ProblemStatus.NEW
    assert cs.reproduction["P-0001"].to_dict() == {"strategy": "immediate", "attempts": 0, "reproduced": True}


def test_stored_pending_problems_follow_the_current_strategy(tmp_path):
    world = make_world(tmp_path)
    earlier = collect_run(1, aggregated_at=NOW)
    runs.save(world.conn, earlier)
    signal = make_signal(1, run_id=earlier.id, check="unsupported_method", location="TRACE /api/Order")
    signals.save(world.conn, signal)
    save_problem(world.conn, "P-0001", fingerprint_of(world, signal), status=ProblemStatus.PENDING,
                 scope=ProblemScope("TRACE /api/Order", roles=frozenset({"Admin"})))
    problems.add_signals(world.conn, "P-0001", [signal.id])
    run = collect_run(2)
    cs = grouped(world, run, [])
    reproduce_step(cs, world, lambda signal, attempts: [None] * attempts)
    assert cs.problems["P-0001"].status is ProblemStatus.NEW


def test_intermittent_pending_problems_are_not_replayed_and_become_new_when_seen_again(tmp_path):
    world = make_world(tmp_path)
    first = collect_run(1)
    cs = grouped(world, first, [make_signal(1, run_id=first.id)])
    reproduce_step(cs, world, lambda signal, attempts: [False, False])
    assert (cs.problems["P-0001"].status, cs.problems["P-0001"].intermittent) == (ProblemStatus.PENDING, True)
    apply.commit(world.conn, cs, world.clock, world.layout, side_effects=False)
    quiet = collect_run(2)
    cs = grouped(world, quiet, [])
    replays = []
    reproduce_step(cs, world, lambda signal, attempts: replays.append(signal) or [True, True])
    assert replays == [] and cs.changes == []
    again = collect_run(3)
    cs = grouped(world, again, [make_signal(2, run_id=again.id)])
    reproduce_step(cs, world, lambda signal, attempts: replays.append(signal) or [True, True])
    assert replays == []
    assert [(change.problem_id, change.to_status, change.event) for change in cs.changes] == [
        ("P-0001", ProblemStatus.NEW, ProblemEvent.PROMOTED)]


def run_status(world, cs, run, known):
    facts = commit_facts.query(Ancestry(known), world.root, status.commit_pairs(cs, world.conn, run))
    status.apply(cs, world.conn, run, facts, NOW, 3)


def test_resolved_problems_regress_only_on_newer_commits(tmp_path):
    world = make_world(tmp_path)
    signal = make_signal(1)
    save_problem(world.conn, "P-0001", fingerprint_of(world, signal), status=ProblemStatus.RESOLVED,
                 resolved_release=RELEASE, issue_id="0007")
    old = collect_run(1)
    cs = grouped(world, old, [replace(signal, run_id=old.id)])
    run_status(world, cs, old, {})
    assert cs.problems["P-0001"].status is ProblemStatus.RESOLVED and cs.reopened == []
    newer = collect_run(2, commit=NEWER)
    cs = grouped(world, newer, [replace(signal, run_id=newer.id, release=NEWER)])
    run_status(world, cs, newer, {(RELEASE, NEWER): True})
    assert cs.problems["P-0001"].status is ProblemStatus.REGRESSED
    assert cs.reopened[0].issue_id == "0007"
    unknown = collect_run(3, commit="f" * 40)
    cs = grouped(world, unknown, [replace(signal, run_id=unknown.id, release="f" * 40)])
    run_status(world, cs, unknown, {})
    assert cs.problems["P-0001"].status is ProblemStatus.RESOLVED
    assert cs.notes == ["P-0001：commit 的先后关系未知，本次不做回归判定"]


def test_covered_runs_on_newer_commits_resolve_problems(tmp_path):
    world = make_world(tmp_path)
    save_problem(world.conn, "P-0001", "a" * 16, scope=ProblemScope("GET /api/Order/{id}", frozenset({"Admin"})))
    save_problem(world.conn, "P-0002", "b" * 16, probe=Probe.STATIC,
                 scope=ProblemScope("src/OrderService.cs:OrderService.Get"))
    for number in range(1, 4):
        run = collect_run(number, commit=NEWER)
        cs = grouped(world, run, [])
        run_status(world, cs, run, {(RELEASE, NEWER): True})
        apply.commit(world.conn, cs, world.clock, world.layout, side_effects=False)
    resolved = problems.get(world.conn, "P-0001")
    assert (resolved.status, resolved.resolved_release) == (ProblemStatus.RESOLVED, NEWER)
    static = collect_run(4, Probe.STATIC, commit=NEWER, coverage=Coverage(files=("src/OrderService.cs",)))
    cs = grouped(world, static, [])
    run_status(world, cs, static, {(RELEASE, NEWER): True})
    assert cs.changes[0].event is ProblemEvent.RESOLVED_ON_NEW_COMMIT


def test_ignore_expires_by_date(tmp_path):
    world = make_world(tmp_path)
    until = IgnoreCondition(until=datetime(2026, 10, 1, tzinfo=timezone.utc))
    later = IgnoreCondition(until=datetime(2026, 11, 1, tzinfo=timezone.utc))
    save_problem(world.conn, "P-0001", "a" * 16, status=ProblemStatus.IGNORED, ignore_until=until)
    save_problem(world.conn, "P-0002", "b" * 16, status=ProblemStatus.IGNORED, ignore_until=later)
    run = collect_run(1, Probe.ALERTS, coverage=Coverage(sources=("alert-source",)))
    cs = grouped(world, run, [])
    run_status(world, cs, run, {})
    assert [(change.problem_id, change.to_status) for change in cs.changes] == [("P-0001", ProblemStatus.NEW)]


def test_platform_group_signals_share_one_problem(tmp_path):
    world = make_world(tmp_path)
    run = collect_run(1, Probe.PLATFORM_ERRORS, coverage=Coverage(sources=("error-tracking",)))
    found = [make_signal(number, run_id=run.id, probe=Probe.PLATFORM_ERRORS, check="error",
                         location=location, context={"platformGroup": "sentry:acme/42", "sourceName": "error-tracking"})
             for number, location in ((1, "app.py:run"), (2, "app.py:other"))]
    cs = grouped(world, run, found)
    [problem_id] = cs.created
    assert cs.problems[problem_id].fingerprint == "sentry:acme/42"
    assert cs.problems[problem_id].scope.source == "error-tracking" and cs.problems[problem_id].occurrences == 2
