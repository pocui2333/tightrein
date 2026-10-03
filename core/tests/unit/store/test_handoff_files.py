import json
from datetime import timedelta

import pytest

from tightrein.contracts.validate import SchemaValidationError
from tightrein.domain.enums import HandoffStatus, RunStage, VerifyPhase
from tightrein.store.files import handoff_files
from tightrein.store.files.handoff_files import HandoffError
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import handoffs

RUN = "R-20260929-021503-loop"


def loop_document(conclusion="本轮完成", **fields):
    document = {
        "schemaVersion": 2, "runId": RUN, "stage": "loop", "subject": {"type": "run", "id": RUN}, "status": "ok",
        "inputsRef": {}, "outputs": {"conclusion": conclusion, "waiting": [], "steps": [], "anomalies": []},
        "nextAction": "无", "blockedReason": None, "createdAt": "2026-09-29T02:30:00Z",
    }
    document.update(fields)
    return document


def failed_document(stage, subject, **fields):
    return loop_document(stage=stage, subject=subject, status="failed", outputs={}, blockedReason="程序错误",
                         **fields)


@pytest.fixture
def layout(tmp_path):
    return WorkspaceLayout(tmp_path / "sample")


def test_names_follow_the_stage_and_subject():
    assert handoff_files.handoff_name(loop_document()) == f"loop-{RUN}"
    triage = failed_document("triage", {"type": "problem", "id": "P-0042"})
    assert handoff_files.handoff_name(triage) == "triage-P-0042"
    verify = failed_document("verify", {"type": "issue", "id": "0007"})
    assert handoff_files.handoff_name(verify, VerifyPhase.LOCAL) == "verify-local-0007"
    weekly = failed_document("learn", {"type": "week", "id": "2026-10-05"})
    assert handoff_files.handoff_name(weekly) == "learn-weekly-2026-10-05"
    with pytest.raises(HandoffError):
        handoff_files.handoff_name(verify)
    with pytest.raises(HandoffError):
        handoff_files.handoff_name(triage, VerifyPhase.LOCAL)


def test_write_creates_the_file_and_the_index(layout, conn, clock):
    written = handoff_files.write(layout, loop_document(), clock, conn=conn)
    assert written.path == layout.handoff(RUN, f"loop-{RUN}")
    assert written.previous is None
    assert json.loads(written.path.read_text(encoding="utf-8")) == loop_document()
    record = handoffs.get(conn, RunStage.LOOP, RUN)
    assert record == written.record
    assert (record.path, record.status, record.schema_version, record.created_at) == (
        f"data/runs/{RUN}/handoff/loop-{RUN}.json", HandoffStatus.OK, 2, clock.now())


def test_rewriting_keeps_numbered_history(layout, conn, clock):
    handoff_files.write(layout, loop_document("第一次"), clock, conn=conn)
    second = handoff_files.write(layout, loop_document("第二次"), clock, conn=conn)
    clock.advance(timedelta(minutes=1))
    third = handoff_files.write(layout, loop_document("第三次"), clock, conn=conn)
    name = f"loop-{RUN}"
    assert second.previous == layout.handoff_history(RUN, name, 1)
    assert third.previous == layout.handoff_history(RUN, name, 2)
    assert handoff_files.history(layout, RUN, name) == [second.previous, third.previous]
    conclusions = [handoff_files.read(path)["outputs"]["conclusion"]
                   for path in (second.previous, third.previous, third.path)]
    assert conclusions == ["第一次", "第二次", "第三次"]
    assert len(handoffs.for_subject(conn, RUN)) == 1
    assert handoffs.get(conn, RunStage.LOOP, RUN).created_at == clock.now()


def test_rerun_in_a_new_run_moves_the_index(layout, conn, clock):
    first = failed_document("triage", {"type": "problem", "id": "P-0042"})
    handoff_files.write(layout, first, clock, conn=conn)
    rerun = failed_document("triage", {"type": "problem", "id": "P-0042"}, runId="R-20260930-010000-triage")
    written = handoff_files.write(layout, rerun, clock, conn=conn)
    assert written.previous is None
    assert layout.handoff(RUN, "triage-P-0042").exists()
    stored = handoffs.for_subject(conn, "P-0042")
    assert [(item.run_id, item.path) for item in stored] == [
        ("R-20260930-010000-triage", "data/runs/R-20260930-010000-triage/handoff/triage-P-0042.json")]


def test_attempts_and_phases_are_separate_records(layout, conn, clock):
    triage = failed_document("triage", {"type": "problem", "id": "P-0042"})
    handoff_files.write(layout, triage, clock, conn=conn, attempt=1)
    handoff_files.write(layout, triage, clock, conn=conn, attempt=2)
    verify = failed_document("verify", {"type": "issue", "id": "0007"})
    handoff_files.write(layout, verify, clock, conn=conn, phase=VerifyPhase.LOCAL)
    handoff_files.write(layout, verify, clock, conn=conn, phase=VerifyPhase.STAGING)
    assert [item.attempt for item in handoffs.for_subject(conn, "P-0042")] == [1, 2]
    assert handoffs.get(conn, RunStage.VERIFY, "0007", phase=VerifyPhase.STAGING).path.endswith(
        "verify-staging-0007.json")


def test_invalid_documents_are_not_written(layout, conn, clock):
    broken = loop_document(outputs={"conclusion": "缺少其余字段"})
    with pytest.raises(SchemaValidationError, match=r"\$\.outputs"):
        handoff_files.write(layout, broken, clock, conn=conn)
    with pytest.raises(SchemaValidationError, match="runId"):
        handoff_files.write(layout, loop_document(runId="run-1"), clock, conn=conn)
    assert not layout.handoff_dir(RUN).exists()
    assert handoffs.for_run(conn, RUN) == []


def test_only_the_current_version_is_written(layout, clock):
    with pytest.raises(HandoffError, match="第 2 版"):
        handoff_files.write(layout, loop_document(schemaVersion=3), clock)


def test_output_mode_writes_files_only(tmp_path, conn, clock):
    layout = WorkspaceLayout(tmp_path / "sample", output_dir=tmp_path / "out")
    written = handoff_files.write(layout, loop_document(), clock)
    assert written.path == tmp_path / "out" / "handoff" / f"loop-{RUN}.json"
    assert written.record is None
    with pytest.raises(HandoffError, match="--output"):
        handoff_files.write(layout, loop_document(), clock, conn=conn)


def test_read_rejects_invalid_files(layout, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(loop_document(status="blocked")), encoding="utf-8")
    with pytest.raises(SchemaValidationError, match="blockedReason"):
        handoff_files.read(path)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(SchemaValidationError):
        handoff_files.read(path)
