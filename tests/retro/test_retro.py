from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tightrein.agents.params import CallParams
from tightrein.agents.result import CallResult, CallStatus
from tightrein.protocol.handoff import Handoff, Metrics, Status, Tokens, read, write
from tightrein.protocol.naming import FileName, FixedClock
from tightrein.protocol.records import EventLog
from tightrein.protocol.security import Redactor
from tightrein.retro import records
from tightrein.retro.records import RecordStatus
from tightrein.retro.retro import retro
from tightrein.settings.load import Settings
from tightrein.store.db import open_database
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout
from tightrein.store.tables import runs
from tightrein.store.tables.runs import Run

REPO_ROOT = Path(__file__).resolve().parents[2]
FIRST = "R-20261007T093000Z-run"
SECOND = "R-20261008T093000Z-run"


@dataclass
class FakeInvoke:
    outputs: list[CallResult]
    prompts: list[str] = field(default_factory=list)

    def __call__(self, params: CallParams, context: object) -> CallResult:
        assert params.point == "retro.idea" and params.subject is None
        self.prompts.append(params.prompt)
        return self.outputs.pop(0)


def ok(idea: str, settle: str | None = None) -> CallResult:
    return CallResult(CallStatus.OK, "claude", "opus", output={"idea": idea, "settle": settle},
                      tokens=Tokens(input=1000, output=100))


def runtime(tmp_path: Path, run: str, when: datetime) -> SimpleNamespace:
    layout = WorkspaceLayout(tmp_path)
    clock = FixedClock(when)
    conn = open_database(layout.database, clock=clock)
    runs.start(conn, Run(run, "run", "schedule", "running", when))
    events = EventLog(layout.events(run), Redactor(), clock)
    return SimpleNamespace(workspace=layout, settings=Settings.load(ToolLayout(REPO_ROOT)), conn=conn, run=run,
                           clock=clock, events=events, agents=object(), language="zh")


def failed_step(context: SimpleNamespace, subject: str, facts: dict[str, Any] | None = None) -> None:
    handoff = Handoff("implement.check", subject, context.run, Status.FAILED, "检查命令跑不起来", facts or {},
                      Metrics(calls=0, tokens=Tokens()), created_at="2026-10-07T09:40:00Z")
    write(context.workspace.step_file(subject, FileName("implement.check", "handoff", "json", round=1)), handoff)
    context.events.emit(run=context.run, subject=subject, point="implement.check", kind="effect", summary="失败")


def test_a_new_problem_gets_a_record_an_idea_and_a_handoff(tmp_path: Path) -> None:
    context = runtime(tmp_path, FIRST, datetime(2026, 10, 7, 9, 30, tzinfo=UTC))
    failed_step(context, "0018")
    failed_step(context, "0019")
    invoke = FakeInvoke([ok("把检查命令的可用性放进接入检查", "接入时先跑一次检查命令")])
    outcome = retro(context, invoke=invoke)  # type: ignore[arg-type]
    assert outcome.created == ["0001"] and outcome.appended == [] and outcome.ratings == {"0001": "P0"}
    assert outcome.errors == [] and outcome.metrics.calls == 1 and outcome.metrics.tokens.input == 1000
    (record,) = records.load_all(context.workspace)
    assert record.count == 2 and record.subjects == {"0018", "0019"}
    assert (record.idea, record.settle) == ("把检查命令的可用性放进接入检查", "接入时先跑一次检查命令")
    assert record.path is not None and record.path.name == "0001-P0-implement.check-step-failed.md"
    assert "检查命令跑不起来" in invoke.prompts[0]
    handoff = read(context.workspace.step_file(FIRST, FileName("retro.detect", "handoff", "json")))
    assert handoff.facts["created"] == [{"id": "0001", "rating": "P0", "kind": "failure", "point": "implement.check",
                                         "count": 2}]


def test_a_known_problem_is_appended_and_rerated_without_calling_the_model(tmp_path: Path) -> None:
    first = runtime(tmp_path, FIRST, datetime(2026, 10, 7, 9, 30, tzinfo=UTC))
    failed_step(first, "0018")
    retro(first, invoke=FakeInvoke([ok("思路")]))  # type: ignore[arg-type]
    assert records.get(first.workspace, "1").rating == "P1"
    second = runtime(tmp_path, SECOND, datetime(2026, 10, 8, 9, 30, tzinfo=UTC))
    failed_step(second, "0020")
    outcome = retro(second, invoke=FakeInvoke([]))  # type: ignore[arg-type]
    assert outcome.created == [] and outcome.appended == ["0001"] and outcome.ratings == {"0001": "P0"}
    record = records.get(second.workspace, "1")
    assert record.count == 2 and record.last_seen == "2026-10-08T09:30:00Z" and record.idea == "思路"
    assert [path.name for path in second.workspace.retro_dir.iterdir()] == ["0001-P0-implement.check-step-failed.md"]
    again = retro(second, invoke=FakeInvoke([]))  # type: ignore[arg-type]
    assert again.appended == ["0001"] and records.get(second.workspace, "1").count == 2


def test_wontfix_records_stay_quiet_and_done_records_reopen(tmp_path: Path) -> None:
    first = runtime(tmp_path, FIRST, datetime(2026, 10, 7, 9, 30, tzinfo=UTC))
    failed_step(first, "0018")
    misjudged = {"misjudged": {"kind": "false_confirm", "point": "assess.triage", "detail": "复现不了"}}
    failed_step(first, "0019", misjudged)
    retro(first, invoke=FakeInvoke([ok("a"), ok("b")]))  # type: ignore[arg-type]
    records.set_status(first.workspace, "0001", RecordStatus.WONTFIX)
    records.set_status(first.workspace, "0002", RecordStatus.DONE)
    second = runtime(tmp_path, SECOND, datetime(2026, 10, 8, 9, 30, tzinfo=UTC))
    failed_step(second, "0020")
    failed_step(second, "0021", misjudged)
    retro(second, invoke=FakeInvoke([]))  # type: ignore[arg-type]
    statuses = {record.kind.value: record.status for record in records.load_all(second.workspace)}
    assert statuses == {"failure": RecordStatus.WONTFIX, "misjudgment": RecordStatus.OPEN}
    assert [record.id for record in records.listing(second.workspace)] == ["0002"]


def test_a_failed_idea_call_still_creates_the_record(tmp_path: Path) -> None:
    context = runtime(tmp_path, FIRST, datetime(2026, 10, 7, 9, 30, tzinfo=UTC))
    failed_step(context, "0018")
    failure = CallResult(CallStatus.TIMEOUT, "claude", "opus", error="超时")
    outcome = retro(context, invoke=FakeInvoke([failure]))  # type: ignore[arg-type]
    assert outcome.created == ["0001"] and len(outcome.errors) == 1 and "timeout" in outcome.errors[0]
    assert records.get(context.workspace, "1").idea is None


def test_a_clean_run_writes_an_empty_handoff(tmp_path: Path) -> None:
    context = runtime(tmp_path, FIRST, datetime(2026, 10, 7, 9, 30, tzinfo=UTC))
    outcome = retro(context, invoke=FakeInvoke([]))  # type: ignore[arg-type]
    assert (outcome.created, outcome.appended, outcome.status) == ([], [], Status.PASSED)
    handoff = read(context.workspace.step_file(FIRST, FileName("retro.detect", "handoff", "json")))
    assert handoff.facts == {"created": [], "appended": [], "errors": []}
