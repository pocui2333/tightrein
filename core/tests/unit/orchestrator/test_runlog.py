from datetime import timedelta

from pipeline_world import NOW, make_world

from tightrein.domain.enums import RunStage, RunStatus
from tightrein.domain.run import Run
from tightrein.observability import events
from tightrein.orchestrator import runlog
from tightrein.store.repos import runs

TTL = timedelta(minutes=120)


def test_new_runs_are_linked_to_the_loop_run(tmp_path):
    world = make_world(tmp_path)
    old = Run("R-20261005-020000-triage", RunStage.TRIAGE, NOW - timedelta(hours=1), RunStatus.OK)
    runs.save(world.conn, old)
    loop = runlog.begin(world.conn, world.clock, world.events, TTL)
    assert loop.id == "R-20261005-030000-loop" and runs.get(world.conn, loop.id).status is RunStatus.RUNNING
    before = runlog.known_runs(world.conn)
    runs.save(world.conn, Run("R-20261005-030001-triage", RunStage.TRIAGE, NOW, RunStatus.FAILED))
    adopted = runlog.adopt(world.conn, loop, before)
    assert [run.id for run in adopted] == ["R-20261005-030001-triage"]
    assert runs.get(world.conn, "R-20261005-030001-triage").parent_run_id == loop.id
    assert runs.get(world.conn, old.id).parent_run_id is None


def test_final_status_and_gate_events(tmp_path):
    world = make_world(tmp_path)
    loop = runlog.begin(world.conn, world.clock, world.events, TTL)
    runlog.gate(loop, "triage", False, "没有新发现或回归的问题")
    assert runlog.final_status(failed=True, waiting=True) is RunStatus.FAILED
    assert runlog.final_status(failed=False, waiting=True) is RunStatus.BLOCKED
    assert runlog.final_status(failed=False, waiting=False) is RunStatus.OK
    world.clock.advance(timedelta(seconds=5))
    ended = runlog.finish(world.conn, world.clock, loop, RunStatus.OK)
    assert ended.ended_at == NOW + timedelta(seconds=5)
    gates = [event for event in events.read(world.layout.events_log(NOW.date())) if event.operation == "gate"]
    assert (gates[-1].decision, gates[-1].reason, gates[-1].attributes["step"]) == ("跳过", "没有新发现或回归的问题",
                                                                                 "triage")
