from dataclasses import replace
from datetime import timedelta

import pytest

from tightrein.domain.enums import (
    CloseReason,
    IssueStatus,
    Probe,
    ProbeLevel,
    RegressionKind,
    RegressionResult,
    RunStage,
    RunStatus,
)
from tightrein.domain.run import Coverage, Endpoint, Run
from tightrein.pipeline.collect.steps import handoff, persist, regressions, run_probe
from tightrein.sources.base import ProbeOutcome, ProbeTarget
from tightrein.store.repos import regressions as regression_repo
from tightrein.store.repos import pending_claims, runs, signals, source_cursors
from tightrein.store.repos.regressions import RegressionCheck
from tightrein.store.repos.pending_claims import PendingClaim
from tightrein.store.repos.source_cursors import SourceCursor
from pipeline_world import NOW, RELEASE, RUN_ID, CountingRandom, make_signal, make_world, save_issue
from store_problem import save_problem


def test_selectors_map_to_probe_options(tmp_path):
    world = make_world(tmp_path)
    options = run_probe.options_for(world.conn, Probe.API_FUZZ,
                                    selectors=["role:Admin", "role:Guest", "path:/api/Order", "path:/api/Item"])
    assert options.roles == ("Admin", "Guest")
    assert options.include_paths == ("/api/Order", "/api/Item")
    assert run_probe.options_for(world.conn, Probe.PROJECT_PROBE, selectors=["name:a", "name:b"]).names == ("a", "b")


@pytest.mark.parametrize("probe, value", [
    (Probe.PLATFORM_ERRORS, "role:Admin"), (Probe.API_FUZZ, "case:x"), (Probe.ALERTS, "path:/a"),
    (Probe.API_FUZZ, "pending:low"), (Probe.STATIC, "bad"),
])
def test_unsupported_selectors_are_rejected(probe, value):
    with pytest.raises(ValueError):
        run_probe.parse_selectors(probe, [value])


def test_static_queues_pending_claims(tmp_path):
    world = make_world(tmp_path)
    low = PendingClaim("PC-1", {"file": "a.py"}, "low", pending_claims.LOW, "R-1", NOW)
    over = replace(low, id="PC-2", severity="high", reason=pending_claims.OVER_LIMIT)
    for item in (low, over):
        pending_claims.save(world.conn, item)
    assert run_probe.options_for(world.conn, Probe.STATIC).queued == (over,)
    assert run_probe.options_for(world.conn, Probe.STATIC, selectors=["pending:low"]).queued == (low,)
    assert run_probe.options_for(world.conn, Probe.STATIC, selectors=["pending:PC-2"]).queued == (over,)
    with pytest.raises(ValueError, match="PC-9"):
        run_probe.options_for(world.conn, Probe.STATIC, selectors=["pending:PC-9"])


def test_static_starts_after_the_last_successful_static_run(tmp_path):
    world = make_world(tmp_path)
    assert run_probe.options_for(world.conn, Probe.STATIC).base_commit is None
    for number, (commit, status) in enumerate([("c1", RunStatus.OK), ("c2", RunStatus.OK), ("c3", RunStatus.FAILED)]):
        runs.save(world.conn, Run(f"R-2026100{number + 1}-030000-collect-static", RunStage.COLLECT,
                                  NOW - timedelta(days=5 - number), status, probe=Probe.STATIC, target_commit=commit))
    options = run_probe.options_for(world.conn, Probe.STATIC, reviewer=object())
    assert options.base_commit == "c2" and options.reviewer is not None


class FakeRunner:
    def __init__(self, results):
        self.results = results
        self.checks = []

    def run_checks(self, checks, target):
        self.checks = list(checks)
        return [regressions.RegressionOutcome(check, self.results[check.check_id], "GET /api/Order/{id}", "返回 500")
                for check in checks]


def target(world, tmp_path):
    return ProbeTarget("staging", RUN_ID, tmp_path / "raw", world.clock, release=RELEASE)


def save_check(conn, issue_id, check_id, kind):
    regression_repo.save(conn, RegressionCheck(issue_id, check_id, kind, f"regressions/{issue_id}/{check_id}", "h"))


def test_failed_checks_of_fixed_issues_become_regression_signals(tmp_path):
    world = make_world(tmp_path)
    save_issue(world.conn, "0007", problems=("P-0001",))
    save_issue(world.conn, "0008", close_reason=CloseReason.WONT_FIX)
    save_issue(world.conn, "0009", status=IssueStatus.TODO)
    save_problem(world.conn, "P-0001", "a1b2c3d4e5f60718", issue_id="0007")
    for issue_id in ("0007", "0008", "0009"):
        save_check(world.conn, issue_id, "api-1", RegressionKind.API)
    save_check(world.conn, "0007", "page-1", RegressionKind.PAGE)
    runner = FakeRunner({"api-1": RegressionResult.FAILED})
    step = regressions.execute(world.conn, Probe.API_FUZZ, target(world, tmp_path), runner, world.redactor,
                               CountingRandom())
    assert [(check.issue_id, check.check_id) for check in runner.checks] == [("0007", "api-1")]
    [signal] = step.signals
    assert (signal.check, signal.location, signal.release, signal.probe) == (
        "regression", "GET /api/Order/{id}", RELEASE, Probe.API_FUZZ)
    assert signal.message == "复现检查 0007/api-1 失败：返回 500"
    assert signal.context == {"issue": "0007", "checkId": "api-1", "targetFingerprints": ["a1b2c3d4e5f60718"],
                              "detail": "返回 500"}


def test_without_runner_or_for_other_probes_no_check_runs(tmp_path):
    world = make_world(tmp_path)
    save_issue(world.conn)
    save_check(world.conn, "0007", "api-1", RegressionKind.API)
    step = regressions.execute(world.conn, Probe.API_FUZZ, target(world, tmp_path), None, world.redactor)
    assert step.notes == (regressions.NO_RUNNER,) and step.outcomes == ()
    assert regressions.execute(world.conn, Probe.PLATFORM_ERRORS, target(world, tmp_path), None, world.redactor) == \
        regressions.RegressionStep()


def ended_run(**changes):
    values = dict(id=RUN_ID, stage=RunStage.COLLECT, started_at=NOW, status=RunStatus.OK, probe=Probe.PLATFORM_ERRORS,
                  level=None, ended_at=NOW + timedelta(minutes=5), target_commit=RELEASE,
                  coverage=Coverage(sources=("log-platform",)))
    values.update(changes)
    return Run(**values)


def test_persist_writes_everything_in_one_transaction(tmp_path):
    world = make_world(tmp_path)
    save_issue(world.conn)
    save_check(world.conn, "0007", "api-1", RegressionKind.API)
    check = regression_repo.get(world.conn, "0007", "api-1")
    cursor = SourceCursor("source-1", {"until": "2026-10-05T03:00:00Z"}, NOW)
    outcome = ProbeOutcome(RunStatus.OK, cursors=(cursor,))
    outcomes = [regressions.RegressionOutcome(check, RegressionResult.PASSED, "GET /a")]
    persist.commit(world.conn, ended_run(), outcome, [make_signal(1), make_signal(2)], outcomes)
    assert len(signals.find(world.conn, run_id=RUN_ID)) == 2
    assert runs.get(world.conn, RUN_ID).coverage.sources == ("log-platform",)
    assert regression_repo.get(world.conn, "0007", "api-1").last_result is RegressionResult.PASSED
    assert source_cursors.get(world.conn, "source-1") == cursor
    persist.commit(world.conn, ended_run(level=ProbeLevel.SHALLOW), ProbeOutcome(RunStatus.OK), [make_signal(3)], [],
                   replace=True)
    assert [signal.id for signal in signals.find(world.conn, run_id=RUN_ID)] == [make_signal(3).id]


def test_failure_midway_leaves_no_signals_and_keeps_the_cursor(tmp_path, monkeypatch):
    world = make_world(tmp_path)
    old = SourceCursor("source-1", {"until": "2026-10-05T02:00:00Z"}, NOW)
    source_cursors.save(world.conn, old)

    def broken(conn, outcome):
        raise RuntimeError("磁盘已满")

    monkeypatch.setattr(persist, "save_state", broken)
    with pytest.raises(RuntimeError):
        moved = replace(old, cursor={"until": "2026-10-05T03:00:00Z"})
        persist.commit(world.conn, ended_run(), ProbeOutcome(RunStatus.OK, cursors=(moved,)), [make_signal(1)], [])
    assert signals.find(world.conn, run_id=RUN_ID) == []
    assert runs.get(world.conn, RUN_ID) is None
    assert source_cursors.get(world.conn, "source-1") == old


def test_handoff_counts_each_endpoint_once_across_roles():
    tested = tuple(Endpoint(method, route, role) for method, route in (("GET", "/a"), ("POST", "/b"))
                   for role in ("anonymous", "learner"))
    counts = handoff.coverage_counts(ended_run(coverage=Coverage(endpoints=tested, endpoints_total=3)))
    assert (counts["endpoints"], counts["endpointsTotal"]) == (2, 3)
