import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tightrein.protocol.handoff import Handoff, Metrics, Status, Tokens, write
from tightrein.protocol.naming import FileName, FixedClock
from tightrein.retro import detect as detect_module
from tightrein.retro.detect import RunData, Thresholds, detect, gather
from tightrein.retro.records import Impact, Kind
from tightrein.store.db import open_database
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.tables import runs
from tightrein.store.tables.runs import Run

STARTED = datetime(2026, 10, 7, 9, 30, tzinfo=UTC)
RUN = "R-20261007T093000Z-run"
EARLIER = "R-20261006T093000Z-run"
THRESHOLDS = Thresholds(step_tokens=300_000, big_tokens=1_000_000, issue_tokens=1_000_000, step_duration_ms=1_200_000,
                        call_duration_ms=600_000, rounds=2, lines_read=5000, cache_read_weight=0.1)


def handoff(point: str, subject: str, *, run: str = RUN, status: Status = Status.PASSED, tokens: int = 0,
            rounds: int | None = None, duration_ms: int | None = None, round: int | None = None,
            facts: dict | None = None, created_at: str = "2026-10-07T09:40:00Z") -> Handoff:
    return Handoff(point, subject, run, status, f"{point} 的结论", facts or {},
                   Metrics(duration_ms=duration_ms, calls=2, rounds=rounds, tokens=Tokens(input=tokens)),
                   round=round, created_at=created_at)


def put(layout: WorkspaceLayout, item: Handoff) -> None:
    write(layout.step_file(item.subject, FileName(item.point, "handoff", "json", round=item.round)), item)


def started(layout: WorkspaceLayout, point: str, subject: str | None, status: str | None, duration_ms: int | None,
            *, run: str = RUN, sequence: int = 37) -> None:
    path = layout.step_file(subject or run, FileName(point, "started", "json", sequence=sequence))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"point": point, "subject": subject, "run": run, "tool": "claude", "model": "opus",
                                "startedAt": "2026-10-07T09:31:00Z",
                                "endedAt": None if status is None else "2026-10-07T09:41:00Z",
                                "status": status, "durationMs": duration_ms}), encoding="utf-8")


def events(layout: WorkspaceLayout, *subjects: str) -> None:
    path = layout.events(RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"run": RUN, "subject": subject, "point": "x", "kind": "action"}) for subject in subjects]
    path.write_text("\n".join([*lines, "不是 JSON"]) + "\n", encoding="utf-8")


@pytest.fixture
def layout(tmp_path: Path) -> WorkspaceLayout:
    return WorkspaceLayout(tmp_path)


def database(layout: WorkspaceLayout, status: str = "running"):  # type: ignore[no-untyped-def]
    conn = open_database(layout.database, clock=FixedClock(STARTED))
    runs.start(conn, Run(RUN, "run", "schedule", "running", STARTED))
    if status != "running":
        runs.finish(conn, RUN, status, FixedClock(STARTED))
    return conn


def test_gather_reads_only_this_runs_records(layout: WorkspaceLayout) -> None:
    conn = database(layout)
    events(layout, "0018")
    put(layout, handoff("implement.code", "0018", run=EARLIER, tokens=800_000, round=1))
    put(layout, handoff("implement.code", "0018", tokens=10, round=2))
    put(layout, handoff("implement.review", "0019", tokens=10))  # 本次没碰过的对象不读
    started(layout, "implement.review.deep", "0018", "timeout", 1000)
    started(layout, "implement.review.deep", "0018", "ok", 1000, run=EARLIER, sequence=36)
    data = gather(layout, conn, RUN, cache_read_weight=0.1)
    assert [(item.point, item.round) for item in data.handoffs] == [("implement.code", 2)]
    assert data.issue_tokens_before == {"0018": 800_000}
    assert [(call.point, call.status) for call in data.calls] == [("implement.review.deep", "timeout")]
    assert data.run_status == "running"
    assert len(data.errors) == 1 and "读不懂" in data.errors[0]


def test_failures_waste_and_interruptions_are_found(layout: WorkspaceLayout) -> None:
    data = RunData(RUN, "failed", issue_tokens_before={"0018": 900_000})
    data.handoffs = [
        handoff("implement.check", "0018", status=Status.FAILED),
        handoff("implement.approve", "0018", status=Status.PENDING),
        handoff("implement.code", "0018", tokens=1_500_000, rounds=4, duration_ms=1_500_000),
    ]
    data.calls = [
        detect_module.CallMark("implement.review.deep", "0018", "quota_exhausted", 1000, "flash"),
        detect_module.CallMark("implement.locate", "0018", None, None, "flash"),
        detect_module.CallMark("implement.code", "0018", "ok", 700_000, "opus"),
    ]
    found = {(item.kind, item.point, item.phenomenon): item for item in detect(data, THRESHOLDS, blamed=set()).findings}
    assert found[(Kind.FAILURE, "run", "run-failed")].impact is Impact.SEVERE
    assert found[(Kind.FAILURE, "implement.check", "step-failed")].impact is Impact.SEVERE
    assert found[(Kind.INTERRUPTION, "implement.approve", "waiting-user")].impact is Impact.TRIVIAL
    tokens = found[(Kind.WASTE, "implement.code", "step-tokens")]
    assert tokens.impact is Impact.MAJOR and tokens.tokens == 1_200_000
    assert "共 2 次调用、1.5M token" in tokens.detail
    assert found[(Kind.WASTE, "implement.code", "many-rounds")].rounds == 2
    assert (Kind.WASTE, "implement.code", "step-slow") in found
    assert found[(Kind.FAILURE, "implement.review", "call-quota-exhausted")].impact is Impact.SEVERE
    assert found[(Kind.FAILURE, "implement.locate", "call-unfinished")].call_point == "implement.locate"
    assert (Kind.WASTE, "implement.code", "call-slow") in found
    crossed = found[(Kind.WASTE, "implement", "issue-tokens")]
    assert crossed.impact is Impact.MAJOR and crossed.tokens == 1_400_000


def test_issue_tokens_are_recorded_only_in_the_run_that_crosses(layout: WorkspaceLayout) -> None:
    data = RunData(RUN, "done", issue_tokens_before={"0018": 1_200_000})
    data.handoffs = [handoff("implement.code", "0018", tokens=200_000)]
    assert not [item for item in detect(data, THRESHOLDS, blamed=set()).findings if item.phenomenon == "issue-tokens"]


def test_an_override_after_a_false_refute_is_written_from_the_earlier_record() -> None:
    misjudged = {"misjudged": {"kind": "false_refute", "point": "assess.triage", "detail": "用户改判为成立"}}
    data = RunData(RUN, "done")
    data.handoffs = [handoff("assess.issue", "P-0003", facts=misjudged),
                     handoff("assess.issue", "P-0004", facts=misjudged),
                     handoff("assess.issue", "P-0005", facts={"misjudged": {"kind": "false_confirm",
                                                                            "point": "assess.triage"}})]
    found = detect(data, THRESHOLDS, blamed={"P-0003"}).findings
    assert [(item.subject, item.phenomenon, item.point) for item in found] == [
        ("P-0004", "false-refute", "assess.triage"), ("P-0005", "false-confirm", "assess.triage")]
    assert all(item.kind is Kind.MISJUDGMENT and item.impact is Impact.MAJOR for item in found)


def test_one_failing_check_does_not_stop_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: object) -> list:
        raise KeyError("tokens")

    monkeypatch.setattr(detect_module, "_step_findings", broken)
    data = RunData(RUN, "failed")
    data.handoffs = [handoff("implement.check", "0018", status=Status.FAILED)]
    result = detect(data, THRESHOLDS, blamed=set())
    assert [item.phenomenon for item in result.findings] == ["run-failed"]
    assert len(result.errors) == 1 and result.errors[0].startswith("步骤检测出错")


def test_an_unreadable_handoff_is_skipped_with_an_error(layout: WorkspaceLayout) -> None:
    conn = database(layout, "failed")
    events(layout, "0018")
    put(layout, handoff("implement.code", "0018", status=Status.FAILED))
    broken = layout.step_file("0018", FileName("implement.review", "handoff", "json"))
    broken.write_text("{", encoding="utf-8")
    data = gather(layout, conn, RUN, cache_read_weight=0.1)
    assert [item.point for item in data.handoffs] == ["implement.code"]
    assert any(str(broken) in error for error in data.errors)
    assert {item.phenomenon for item in detect(data, THRESHOLDS, blamed=set()).findings} == {"run-failed",
                                                                                              "step-failed"}
