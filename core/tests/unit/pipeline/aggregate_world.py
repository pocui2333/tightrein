"""aggregate 服务测试共用的运行、信号与服务构造。"""

from datetime import timedelta

from tightrein.domain.enums import Probe, RunStage, RunStatus
from tightrein.domain.run import Coverage, Endpoint, EnvironmentDetail, HealthCheck, Run
from tightrein.pipeline.aggregate.service import AggregateDeps, AggregateService
from tightrein.store.repos import runs, signals
from pipeline_world import NOW, RELEASE, make_signal

ENDPOINTS = tuple(Endpoint("GET", path, "Admin") for path in
                  ("/api/Order/{id}", "/api/Item/{id}", "/api/Stock/{id}", "/api/User/{id}"))


def collect_run(hours, probe=Probe.API_FUZZ, commit=RELEASE, **changes):
    started = NOW + timedelta(hours=hours)
    values = dict(id=f"R-{started:%Y%m%d-%H%M%S}-collect-{probe.value}", stage=RunStage.COLLECT, started_at=started,
                  status=RunStatus.OK, probe=probe, ended_at=started + timedelta(minutes=5), target_commit=commit,
                  coverage=Coverage(endpoints=ENDPOINTS),
                  environment_detail=EnvironmentDetail(health=HealthCheck(200)))
    values.update(changes)
    return Run(**values)


def save_run(conn, run, found=()):
    runs.save(conn, run)
    for signal in found:
        signals.save(conn, signal)
    return run


def error_on(number, run, path="/api/Order/42", **changes):
    return make_signal(number, run_id=run.id, location=f"GET {path}", release=run.target_commit,
                       occurred_at=run.started_at + timedelta(minutes=1), **changes)


class Ancestry:
    def __init__(self, known=None):
        self.known = known or {}

    def is_ancestor(self, repo, commit, of):
        from tightrein.vcs.errors import RefNotFound
        if (commit, of) not in self.known:
            raise RefNotFound(commit)
        return self.known[(commit, of)]


def always(result):
    return lambda signal, attempts: [result] * attempts


def make_service(world, replayer=always(True), known=None):
    return AggregateService(AggregateDeps(world.layout, world.config, world.conn, world.clock, world.events,
                                          Ancestry(known), replayer))
