from datetime import timedelta

from pipeline_world import NOW, make_world

from tightrein.domain.enums import RunStage, RunStatus
from tightrein.domain.run import Run
from tightrein.orchestrator import recovery
from tightrein.store import locks
from tightrein.store.locks import Holder
from tightrein.store.repos import runs

HOST = "this-host"
DEAD = 4001
ALIVE = 4002


def running(world, run_id, stage=RunStage.TRIAGE, parent=None):
    run = Run(run_id, stage, NOW, RunStatus.RUNNING, parent_run_id=parent)
    runs.save(world.conn, run)
    return run


def hold(world, subject, run_id, pid):
    locks.acquire(world.conn, subject, world.clock, timedelta(hours=1), run_id=run_id, holder=Holder(pid, HOST))


def recover(world, **options):
    return recovery.recover(world.conn, world.clock, world.events, alive=lambda pid: pid == ALIVE, host=HOST,
                            **options)


def test_runs_whose_lock_holders_died_are_taken_over(tmp_path):
    world = make_world(tmp_path)
    running(world, "R-20261005-020000-triage")
    hold(world, "P-0001", "R-20261005-020000-triage", DEAD)
    running(world, "R-20261005-020100-fix", RunStage.FIX)
    hold(world, "0007", "R-20261005-020100-fix", ALIVE)
    found = recover(world)
    assert found == [recovery.Recovered("R-20261005-020000-triage", ("P-0001",))]
    taken = runs.get(world.conn, "R-20261005-020000-triage")
    assert (taken.status, taken.ended_at) == (RunStatus.INTERRUPTED, NOW)
    assert locks.get(world.conn, "P-0001") is None and locks.get(world.conn, "0007") is not None
    assert runs.get(world.conn, "R-20261005-020100-fix").status is RunStatus.RUNNING


def test_children_of_a_dead_loop_run_are_taken_over_and_other_runs_are_left(tmp_path):
    world = make_world(tmp_path)
    running(world, "R-20261005-020000-loop", RunStage.LOOP)
    hold(world, "R-20261005-020000-loop", "R-20261005-020000-loop", DEAD)
    running(world, "R-20261005-020001-learn", RunStage.LEARN, parent="R-20261005-020000-loop")
    running(world, "R-20261005-020002-issue", RunStage.ISSUE)
    found = recover(world, current="R-20261005-030000-loop")
    assert [item.run_id for item in found] == ["R-20261005-020000-loop", "R-20261005-020001-learn"]
    assert runs.get(world.conn, "R-20261005-020002-issue").status is RunStatus.RUNNING
