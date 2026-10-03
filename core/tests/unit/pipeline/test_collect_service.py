import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from tightrein.contracts.validate import validate_handoff
from tightrein.domain.enums import Probe, ProbeLevel, RunStage, RunStatus
from tightrein.domain.run import Coverage, Endpoint, EnvironmentDetail, Run
from tightrein.pipeline.collect.service import CollectDeps, CollectRequest, CollectService
from tightrein.pipeline.collect.steps.preconditions import HEALTH_UNCONFIGURED
from tightrein.sources.base import ProbeOutcome, failed, skipped
from tightrein.sources.common.session import NO_TARGET
from tightrein.store.repos import handoffs, runs, signals, source_cursors
from tightrein.store.repos.source_cursors import SourceCursor
from tightrein.pipeline.common.deploys import UNCONFIGURED as DEPLOY_UNCONFIGURED
from pipeline_world import (
    NOW,
    RELEASE,
    CountingRandom,
    FakeDeployments,
    FakeRefs,
    FakeTransport,
    deployment_run,
    make_signal,
    make_world,
)


class FakeProbe:
    """按探针给出预设结果；信号的运行编号取本次目标。"""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def __call__(self, kind):
        self.kind = kind
        return self

    def run(self, target, level, options):
        self.calls.append((self.kind, target, level, options))
        outcome = self.outcomes[self.kind]
        return replace(outcome, signals=tuple(replace(signal, run_id=target.run_id, probe=self.kind)
                                              for signal in outcome.signals))


API_OK = ProbeOutcome(
    RunStatus.OK, (make_signal(1), make_signal(2, check="response_schema_conformance")),
    Coverage(endpoints=(Endpoint("GET", "/api/Order/{id}", "Admin"),), endpoints_total=3, methods="GET"),
    EnvironmentDetail(failed_roles=("Guest",)), stats={"seed": 7, "failures": 2},
    extensions={"spec-export": {"implementation": "stack", "cached": True}},
)
LOG_OK = ProbeOutcome(RunStatus.OK, (make_signal(3, check="error", location="OrderService.Get"),),
                      Coverage(sources=("error-tracking",)),
                      cursors=(SourceCursor("platform-errors:error-tracking", {"until": "2026-10-05T03:00:00Z"}, NOW),))


def service(world, probes, transport=None, disabled=None):
    deps = CollectDeps(world.layout, world.config, world.conn, world.clock, world.events, probes,
                       transport or FakeTransport(200), FakeDeployments(deployment_run()), FakeRefs(), world.redactor,
                       randomness=CountingRandom(), disabled=lambda: dict(disabled or {}))
    return CollectService(deps)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_api_fuzz_writes_signals_run_and_handoff(tmp_path):
    world = make_world(tmp_path)
    probes = FakeProbe({Probe.API_FUZZ: API_OK})
    result = service(world, probes, disabled={"alerts": "没有配置 extensions.alert-source"}).run(
        CollectRequest(Probe.API_FUZZ))
    run = runs.get(world.conn, result.run.id)
    assert (run.status, run.level, run.target_commit) == (RunStatus.OK, ProbeLevel.SHALLOW, RELEASE)
    assert run.environment_detail.health.status == 200 and run.environment_detail.failed_roles == ("Guest",)
    assert run.coverage == API_OK.coverage
    assert len(signals.find(world.conn, run_id=run.id)) == 2
    document = read(result.handoff)
    assert validate_handoff(document) == []
    outputs = document["outputs"]
    assert (document["status"], document["nextAction"]) == ("ok", "交给 aggregate")
    assert outputs["signalCount"] == 2 and outputs["signalsByCheck"] == {
        "not_a_server_error": 1, "response_schema_conformance": 1}
    assert outputs["coverage"]["endpoints"] == 1 and outputs["stats"]["extensions"]["spec-export"]["cached"]
    lines = world.layout.signals_file(run.id).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["runId"] for line in lines] == [run.id, run.id]
    assert handoffs.get(world.conn, RunStage.COLLECT, run.id) is not None
    assert outputs["disabledSources"] == {"alerts": "没有配置 extensions.alert-source"}
    report = world.layout.run_report(run.id).read_text(encoding="utf-8")
    assert "未启用的采集方法：alerts(没有配置 extensions.alert-source)" in report
    assert len(probes.calls) == 1


def test_platform_reads_save_their_cursor_with_the_signals(tmp_path):
    world = make_world(tmp_path)
    result = service(world, FakeProbe({Probe.PLATFORM_ERRORS: LOG_OK})).run(CollectRequest(Probe.PLATFORM_ERRORS))
    run = runs.get(world.conn, result.run.id)
    assert run.coverage.sources == ("error-tracking",) and len(signals.find(world.conn, run_id=run.id)) == 1
    assert source_cursors.get(world.conn, "platform-errors:error-tracking").cursor == {"until": "2026-10-05T03:00:00Z"}
    assert read(result.handoff)["outputs"]["coverage"]["sources"] == ["error-tracking"]


def test_partial_writes_signals_and_failed_writes_none(tmp_path):
    world = make_world(tmp_path)
    partial = replace(API_OK, status=RunStatus.PARTIAL, notes=("角色 Guest 登录失败",))
    result = service(world, FakeProbe({Probe.API_FUZZ: partial})).run(CollectRequest(Probe.API_FUZZ))
    assert result.exit_code == 0 and len(signals.find(world.conn, run_id=result.run.id)) == 2
    world.clock.advance(timedelta(minutes=1))
    broken = failed("Schemathesis 未安装", signals=API_OK.signals)
    result = service(world, FakeProbe({Probe.API_FUZZ: broken})).run(CollectRequest(Probe.API_FUZZ))
    assert result.exit_code == 1
    assert signals.find(world.conn, run_id=result.run.id) == []
    document = read(result.handoff)
    assert (document["status"], document["blockedReason"]) == ("failed", "Schemathesis 未安装")


def test_failed_health_check_blocks_without_calling_the_probe(tmp_path):
    world = make_world(tmp_path)
    probes = FakeProbe({})
    result = service(world, probes, FakeTransport(None)).run(CollectRequest(Probe.API_FUZZ))
    assert result.exit_code == 1 and probes.calls == []
    run = runs.get(world.conn, result.run.id)
    assert run.status is RunStatus.BLOCKED and run.environment_detail.health.status is None
    document = read(result.handoff)
    assert validate_handoff(document) == []
    assert document["status"] == "blocked" and document["blockedReason"] == "staging 不可用"


def test_without_health_check_or_deploy_detection_both_are_noted(tmp_path):
    world = make_world(tmp_path, target={"baseUrl": "https://staging.example.test"})
    probes = FakeProbe({Probe.API_FUZZ: API_OK})
    collect = service(world, probes, FakeTransport(None))
    collect.deps.deployments = FakeDeployments(None)
    result = collect.run(CollectRequest(Probe.API_FUZZ))
    assert result.exit_code == 0 and len(probes.calls) == 1
    assert runs.get(world.conn, result.run.id).environment_detail.health is None
    notes = read(result.handoff)["outputs"]["notes"]
    assert HEALTH_UNCONFIGURED in notes and DEPLOY_UNCONFIGURED in notes


def test_without_target_the_probe_runs_without_an_address(tmp_path):
    world = make_world(tmp_path, target=None)
    probes = FakeProbe({Probe.API_FUZZ: skipped(NO_TARGET)})
    result = service(world, probes, FakeTransport(None)).run(CollectRequest(Probe.API_FUZZ))
    assert probes.calls[0][1].base_url is None
    document = read(result.handoff)
    assert (document["status"], document["outputs"]["skippedReason"]) == ("ok", NO_TARGET)
    assert runs.get(world.conn, result.run.id).status is RunStatus.SKIPPED


def test_reparse_replaces_an_unaggregated_run_and_keeps_the_old_handoff(tmp_path):
    world = make_world(tmp_path)
    first = service(world, FakeProbe({Probe.API_FUZZ: API_OK})).run(CollectRequest(Probe.API_FUZZ))
    fewer = replace(API_OK, signals=API_OK.signals[:1])
    probes = FakeProbe({Probe.API_FUZZ: fewer})
    again = service(world, probes, FakeTransport(None)).run(CollectRequest(Probe.API_FUZZ, reparse=first.run.id))
    assert again.run.id == first.run.id
    _, target, level, options = probes.calls[0]
    assert options.from_raw and level is ProbeLevel.SHALLOW
    assert target.raw_dir == world.layout.probe_raw_dir(first.run.id, Probe.API_FUZZ)
    assert len(signals.find(world.conn, run_id=first.run.id)) == 1
    assert (again.handoff.parent / f"collect-{first.run.id}.1.json").is_file()


def test_reparse_of_an_aggregated_run_creates_a_new_run(tmp_path):
    world = make_world(tmp_path)
    first = service(world, FakeProbe({Probe.API_FUZZ: API_OK})).run(CollectRequest(Probe.API_FUZZ))
    runs.mark_aggregated(world.conn, [first.run.id], NOW)
    world.clock.advance(timedelta(minutes=1))
    reparsed = replace(API_OK, signals=(make_signal(5), make_signal(6)))
    again = service(world, FakeProbe({Probe.API_FUZZ: reparsed})).run(
        CollectRequest(Probe.API_FUZZ, reparse=first.run.id))
    assert again.run.id != first.run.id
    assert len(signals.find(world.conn, run_id=first.run.id)) == 2
    assert len(signals.find(world.conn, run_id=again.run.id)) == 2
    assert "aggregate --rebuild" in read(again.handoff)["outputs"]["notes"][-1]


def digest(world):
    return hashlib.sha256(world.layout.database().read_bytes()).hexdigest()


def test_output_mode_leaves_the_database_unchanged(tmp_path):
    output = tmp_path / "out"
    world = make_world(tmp_path, output_dir=output)
    world.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = digest(world)
    result = service(world, FakeProbe({Probe.PLATFORM_ERRORS: LOG_OK})).run(CollectRequest(Probe.PLATFORM_ERRORS))
    world.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert digest(world) == before
    assert runs.find(world.conn) == [] and source_cursors.TABLE.find(world.conn) == []
    assert result.handoff.parent == output / "handoff"
    assert len((output / "signals.ndjson").read_text(encoding="utf-8").splitlines()) == 1


def test_dry_run_writes_nothing(tmp_path):
    world = make_world(tmp_path)
    probes = FakeProbe({})
    result = service(world, probes).run(CollectRequest(Probe.API_FUZZ, dry_run=True))
    plan = result.plan
    assert (plan.probe, plan.level, plan.release) == (Probe.API_FUZZ, ProbeLevel.SHALLOW, RELEASE)
    assert probes.calls == [] and runs.find(world.conn) == []
    assert not world.layout.runs_dir().exists() and not world.layout.logs_dir().exists()


def test_stale_running_runs_are_failed_on_start(tmp_path):
    world = make_world(tmp_path)
    old = Run("R-20261005-000000-collect-alerts", RunStage.COLLECT, NOW - timedelta(hours=3), RunStatus.RUNNING,
              probe=Probe.ALERTS)
    recent = Run("R-20261005-025000-collect-alerts", RunStage.COLLECT, NOW - timedelta(minutes=10),
                 RunStatus.RUNNING, probe=Probe.ALERTS)
    runs.save(world.conn, old)
    runs.save(world.conn, recent)
    service(world, FakeProbe({Probe.INCIDENTAL: ProbeOutcome(RunStatus.OK)})).run(CollectRequest(Probe.INCIDENTAL))
    assert runs.get(world.conn, old.id).status is RunStatus.FAILED
    assert runs.get(world.conn, recent.id).status is RunStatus.RUNNING


def test_boundary_checks(tmp_path):
    world = make_world(tmp_path)
    collect = service(world, FakeProbe({}))
    with pytest.raises(ValueError):
        collect.run(CollectRequest(Probe.ALERTS, import_archive=tmp_path))
    with pytest.raises(ValueError):
        collect.run(CollectRequest(Probe.PLATFORM_ERRORS, select=("role:Admin",)))
    with pytest.raises(LookupError):
        collect.run(CollectRequest(Probe.ALERTS, reparse="R-20261005-000000-collect-alerts"))
    assert runs.find(world.conn) == []


def test_deployment_detection_only(tmp_path):
    world = make_world(tmp_path)
    assert service(world, FakeProbe({})).deployments().commit == RELEASE
    assert runs.find(world.conn) == []
