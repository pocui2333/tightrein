import hashlib
import json
from dataclasses import replace
import pytest

from tightrein.contracts.validate import validate_handoff
from tightrein.domain.clock import FixedClock
from tightrein.domain.enums import (
    HandoffStatus,
    IssueStatus,
    ProblemEvent,
    ProblemStatus,
    RunStage,
    RunStatus,
)
from tightrein.domain.fingerprint import CURRENT_VERSION, fingerprint
from tightrein.domain.normalize import normalize
from tightrein.domain.state_machine import InvalidTransition
from tightrein.pipeline.aggregate.changeset import TransitionRejected
from tightrein.pipeline.aggregate.service import AggregateRequest
from tightrein.pipeline.aggregate.steps import status
from tightrein.pipeline.collect.steps import handoff as collect_handoff
from tightrein.pipeline.collect.steps.target import TargetInfo
from tightrein.store import locks
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.locks import FileLockBusy
from tightrein.store.repos import issues, problem_events, problems, runs, signals
from tightrein.store.repos.problem_events import ProblemEventRecord
from aggregate_world import collect_run, error_on, make_service, save_run
from pipeline_world import NOW, RELEASE, make_world, write_issue_file
from store_problem import save_problem

NEWER = "d" * 40


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def fingerprint_of(signal):
    return fingerprint(replace(signal, normalized_message=normalize(signal.message, ())), CURRENT_VERSION)


def test_runs_are_processed_in_order(tmp_path):
    world = make_world(tmp_path)
    first = collect_run(1)
    later = collect_run(2)
    save_run(world.conn, later, [error_on(9, later)])
    save_run(world.conn, first, [])
    result = make_service(world).run(AggregateRequest())
    assert result.status is HandoffStatus.OK and result.run.status is RunStatus.OK
    [run_document, problem_document] = [read(path) for path in result.handoffs]
    assert all(validate_handoff(document) == [] for document in (run_document, problem_document))
    outputs = run_document["outputs"]
    assert [(item["runId"], item["grouped"]) for item in outputs["processedRuns"]] == [(first.id, 0), (later.id, 1)]
    assert outputs["forTriage"] == [{"problemId": "P-0001", "handoff": "handoff/aggregate-P-0001.json"}]
    assert outputs["counts"]["new"] == 1 and run_document["nextAction"] == "交给 triage"
    assert problem_document["outputs"]["reproduction"] == {"strategy": "replay", "attempts": 2, "reproduced": True}
    assert all(run.aggregated_at is not None for run in runs.find(world.conn, stage=RunStage.COLLECT))
    assert world.layout.run_report(result.run.id).read_text(encoding="utf-8").count("P-0001") == 1


def test_problems_with_issues_are_not_sent_to_triage_again_but_regressions_are(tmp_path):
    world = make_world(tmp_path)
    first = collect_run(1, commit=NEWER)
    ongoing = error_on(1, first)
    regressed = error_on(2, first, "/api/Item/7")
    save_problem(world.conn, "P-0001", fingerprint_of(ongoing), status=ProblemStatus.ONGOING, issue_id="0006")
    save_problem(world.conn, "P-0002", fingerprint_of(regressed), status=ProblemStatus.RESOLVED, issue_id="0007",
                 resolved_release=RELEASE)
    write_issue_file(world, "0007", ("P-0002",))
    save_run(world.conn, first, [ongoing, regressed])
    result = make_service(world, known={(RELEASE, NEWER): True}).run(AggregateRequest())
    outputs = read(result.handoffs[0])["outputs"]
    assert [item["problemId"] for item in outputs["forTriage"]] == ["P-0002"]
    assert outputs["reopenedIssues"] == ["0007"]
    assert read(result.handoffs[1])["outputs"]["issueId"] == "0007"
    assert problems.get(world.conn, "P-0001").occurrences == 2
    assert issues.get(world.conn, "0007").issue.status is IssueStatus.TODO


def write_collect_handoff(tmp_path, run, found):
    layout = WorkspaceLayout(tmp_path / "workspace", tmp_path / "collect-out")
    info = TargetInfo("staging", "https://staging.example.test", run.target_commit, None, None)
    values = collect_handoff.outputs(run, info, None, found, [], notes=[])
    return collect_handoff.write(layout, None, FixedClock(NOW), run, values, found)


def digest(world):
    world.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return hashlib.sha256(world.layout.database().read_bytes()).hexdigest()


def test_input_and_output_leave_the_database_and_files_unchanged(tmp_path):
    world = make_world(tmp_path, output_dir=tmp_path / "out")
    run = collect_run(1)
    path = write_collect_handoff(tmp_path, run, [error_on(1, run)])
    before = digest(world)
    result = make_service(world, replayer=None).run(AggregateRequest(input=path))
    assert digest(world) == before
    assert runs.find(world.conn) == [] and not world.layout.suppressions().exists()
    run_document = read(result.handoffs[0])
    assert result.handoffs[0].parent == tmp_path / "out" / "handoff"
    assert run_document["outputs"]["counts"]["pending"] == 1
    assert "只有覆盖范围的计数" in run_document["outputs"]["notes"][0]
    [changeset] = json.loads((tmp_path / "out" / "changeset.json").read_text(encoding="utf-8"))
    assert changeset["created"] == ["P-0001"] and changeset["processedRuns"][0]["runId"] == run.id


def test_dry_run_lists_runs_without_writing(tmp_path):
    world = make_world(tmp_path)
    run = collect_run(1)
    save_run(world.conn, run, [error_on(1, run)])
    result = make_service(world).run(AggregateRequest(dry_run=True))
    [planned] = result.plan.runs
    assert (planned.run_id, planned.signals, planned.health) == (run.id, 1, 200)
    assert runs.get(world.conn, run.id).aggregated_at is None and problems.find(world.conn) == []
    assert not world.layout.runs_dir().exists()


def test_nothing_to_do(tmp_path):
    world = make_world(tmp_path)
    result = make_service(world).run(AggregateRequest())
    assert (result.message, result.exit_code, result.handoffs) == ("没有新信号", 0, ())


def test_live_reproduction_needs_a_replayer(tmp_path):
    world = make_world(tmp_path)
    with pytest.raises(ValueError):
        make_service(world, replayer=None).run(AggregateRequest())
    with pytest.raises(ValueError):
        make_service(world).run(AggregateRequest(reproduce="maybe"))


def test_interrupted_runs_get_their_handoffs_written(tmp_path):
    world = make_world(tmp_path)
    run = collect_run(1)
    save_run(world.conn, run, [error_on(1, run)])
    save_problem(world.conn, "P-0001", fingerprint_of(error_on(1, run)))
    problems.add_signals(world.conn, "P-0001", [error_on(1, run).id])
    stale = "R-20261005-020000-aggregate"
    runs.save(world.conn, replace(run, id=stale, stage=RunStage.AGGREGATE, probe=None, status=RunStatus.RUNNING))
    problem_events.append(world.conn, ProblemEventRecord("P-0001", NOW, ProblemEvent.REPRODUCED, "auto",
                                                         ProblemStatus.PENDING, ProblemStatus.NEW, run_id=stale))
    make_service(world).run(AggregateRequest(select=("probe:alerts",)))
    document = read(world.layout.handoff(stale, f"aggregate-{stale}"))
    assert document["outputs"]["forTriage"][0]["problemId"] == "P-0001"
    assert world.layout.handoff(stale, "aggregate-P-0001").is_file()
    assert runs.get(world.conn, stale).status is RunStatus.OK


def test_a_rejected_transition_stops_at_that_run(tmp_path, monkeypatch):
    world = make_world(tmp_path)
    first, second = collect_run(1), collect_run(2)
    save_run(world.conn, first, [error_on(1, first)])
    save_run(world.conn, second, [error_on(2, second, "/api/Item/1")])
    calls = []
    original = status.apply

    def rejecting(changeset, *args):
        calls.append(changeset)
        if len(calls) == 2:
            raise TransitionRejected("P-0002", InvalidTransition(ProblemStatus.NEW, ProblemEvent.REPRODUCED))
        return original(changeset, *args)

    monkeypatch.setattr(status, "apply", rejecting)
    result = make_service(world).run(AggregateRequest())
    assert result.exit_code == 1 and result.run.status is RunStatus.FAILED
    document = read(result.handoffs[0])
    assert document["status"] == "failed" and "P-0002" in document["blockedReason"]
    assert runs.get(world.conn, first.id).aggregated_at is not None
    assert runs.get(world.conn, second.id).aggregated_at is None
    assert problems.get(world.conn, "P-0002") is None


def test_a_busy_lock_fails_immediately_with_no_wait(tmp_path):
    world = make_world(tmp_path)
    with locks.file_lock(world.layout.aggregate_lock()):
        with pytest.raises(FileLockBusy):
            make_service(world).run(AggregateRequest(no_wait=True))


def test_blocked_runs_are_only_marked_aggregated(tmp_path):
    world = make_world(tmp_path)
    blocked = collect_run(1, status=RunStatus.BLOCKED,
                          environment_detail=replace(collect_run(1).environment_detail,
                                                     health=replace(collect_run(1).environment_detail.health,
                                                                    status=None)))
    save_run(world.conn, blocked)
    result = make_service(world).run(AggregateRequest())
    outputs = read(result.handoffs[0])["outputs"]
    assert problems.find(world.conn) == []
    assert outputs["forTriage"] == [] and read(result.handoffs[0])["nextAction"] == "无需分诊"
    assert runs.get(world.conn, blocked.id).aggregated_at is not None
