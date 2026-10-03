import json
import os
from datetime import date, datetime, timezone

import pytest
from probe_world import NOW, CountingRandom, make_redactor, make_target

from tightrein.domain.enums import HandoffStatus, Probe, RunStage, RunStatus, Source, VerifyPhase
from tightrein.sources.base import ProbeOptions, save_state
from tightrein.sources.incidental import archive_import, handoff_source
from tightrein.sources.incidental.locate import Location, locate
from tightrein.sources.incidental.probe import IncidentalDependencies, IncidentalProbe
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import handoffs
from tightrein.store.repos.handoffs import HandoffRecord

TRIAGE_COMMIT = "c" * 40
FIX_BASE = "f" * 40


@pytest.mark.parametrize(("text", "expected"), [
    ("`src/Services/CalculateService.cs:210` 中 CalculateService.SaveResult 以 UTC 写入时间",
     Location("src/Services/CalculateService.cs", 210, "CalculateService.SaveResult")),
    ("RefundService.cs 的 RefundService.Retry 重试后不清除人工复核标记",
     Location("RefundService.cs", None, "RefundService.Retry")),
    ("src/vue/src/views/Calculate.vue:88-92 的分页没有重置", Location("src/vue/src/views/Calculate.vue", 88, None)),
    ("PayController.Notify 在 Controllers/PayController.cs 中没有校验签名",
     Location("Controllers/PayController.cs", None, "PayController.Notify")),
    ("OrderService.Page 吞掉了异常", None),
    ("1.5 秒超时，e.g. 这里", None),
])
def test_locate(text, expected):
    assert locate(text) == expected


REPORT = """# 开发报告 2026-09-18

## 结论

- 不是发现：src/Ignored.cs

## 任务外发现

- `src/Services/CalculateService.cs:210` 中 CalculateService.SaveResult 以 UTC 写入，
  前端按本地时间显示
- RefundService.cs 的 RefundService.Retry 重试后不清除人工复核标记
  - 嵌套说明也并入这一条

1. 吞掉异常的 catch：OrderService.Page 没有位置

### 细节

- `src/Detail.cs` 在子标题下仍属于这一节

## 其他

- src/After.cs 不在这一节
"""


def test_archive_items():
    assert list(archive_import.items(REPORT)) == [
        "`src/Services/CalculateService.cs:210` 中 CalculateService.SaveResult 以 UTC 写入， 前端按本地时间显示",
        "RefundService.cs 的 RefundService.Retry 重试后不清除人工复核标记 嵌套说明也并入这一条",
        "吞掉异常的 catch：OrderService.Page 没有位置",
        "`src/Detail.cs` 在子标题下仍属于这一节",
    ]


def test_report_date(tmp_path):
    named = tmp_path / "2026-09-29-report.md"
    named.write_text("# x", encoding="utf-8")
    assert archive_import.report_date(named, "2026-01-01") == date(2026, 9, 29)
    inside = tmp_path / "report.md"
    inside.write_text(REPORT, encoding="utf-8")
    assert archive_import.report_date(inside, REPORT) == date(2026, 9, 18)
    bare = tmp_path / "notes.md"
    bare.write_text("没有日期", encoding="utf-8")
    os.utime(bare, (1790000000, 1790000000))
    assert archive_import.report_date(bare, "没有日期") == date(2026, 9, 21)


def handoff(layout, conn, stage, subject, findings, *, attempt=1, status=HandoffStatus.OK, run="R-20261004-020000",
            stale=False, phase=None):
    run_id = f"{run}-{stage.value}"
    outputs = {"incidentalFindings": findings}
    if stage is RunStage.TRIAGE:
        outputs["triageCommit"] = TRIAGE_COMMIT
    if stage is RunStage.FIX:
        outputs["baseCommit"] = FIX_BASE
    document = {"runId": run_id, "stage": stage.value, "subject": {"type": "problem", "id": subject},
                "status": status.value, "outputs": outputs, "createdAt": "2026-10-04T02:30:00Z"}
    path = layout.handoff(run_id, f"{stage.value}-{subject}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    handoffs.save(conn, HandoffRecord(stage, subject, attempt, run_id, layout.relative(path), status, 1, NOW, phase,
                                      NOW if stale else None))
    return layout.relative(path)


FINDING = {"file": "src/Services/RefundService.cs", "line": 88, "symbol": "RefundService.Retry",
           "text": "退款重试后不清除人工复核标记"}


def test_handoff_candidates(tmp_path, conn):
    layout = WorkspaceLayout(tmp_path / "workspace")
    triage = handoff(layout, conn, RunStage.TRIAGE, "P-0001", [FINDING])
    fix = handoff(layout, conn, RunStage.FIX, "0007", [{"file": "src/A.cs", "line": None, "symbol": None,
                                                       "text": "A 类缺少空值判断"}])
    handoff(layout, conn, RunStage.TRIAGE, "P-0002", [FINDING], status=HandoffStatus.FAILED)
    handoff(layout, conn, RunStage.TRIAGE, "P-0003", [FINDING], stale=True)
    handoff(layout, conn, RunStage.VERIFY, "0008", [FINDING], phase=VerifyPhase.LOCAL)
    reads = handoff_source.unread(conn, layout.root)
    assert [read.path for read in reads] == [fix, triage]
    first = reads[1].findings[0]
    assert (first.location, first.release, first.stage, first.subject) == (
        Location("src/Services/RefundService.cs", 88, "RefundService.Retry"), TRIAGE_COMMIT, "triage", "P-0001")
    assert first.occurred_at == datetime(2026, 10, 4, 2, 30, tzinfo=timezone.utc)
    assert reads[0].findings[0].release == FIX_BASE and reads[0].findings[0].location.text() == "src/A.cs"


def probe(tmp_path, conn):
    layout = WorkspaceLayout(tmp_path / "workspace")
    return layout, IncidentalProbe(IncidentalDependencies(conn, layout, make_redactor(), CountingRandom()))


def run(current, tmp_path, **options):
    return current.run(make_target(tmp_path, "incidental"), None, ProbeOptions(**options))


def test_signals_and_read_records(tmp_path, conn):
    layout, current = probe(tmp_path, conn)
    path = handoff(layout, conn, RunStage.TRIAGE, "P-0001", [FINDING])
    handoff(layout, conn, RunStage.TRIAGE, "P-0002", [])
    outcome = run(current, tmp_path)
    assert outcome.status is RunStatus.OK and len(outcome.signals) == 1
    signal = outcome.signals[0]
    assert (signal.source, signal.probe, signal.check) == (Source.SYNTHETIC, Probe.INCIDENTAL, "incidental")
    assert (signal.location, signal.message, signal.release) == (
        "src/Services/RefundService.cs:RefundService.Retry", "退款重试后不清除人工复核标记", TRIAGE_COMMIT)
    assert signal.context == {"line": 88, "sourcePath": path, "sourceStage": "triage", "sourceSubject": "P-0001",
                              "sourceRunId": "R-20261004-020000-triage", "sourceDate": "2026-10-04"}
    assert [(source.source_path, source.signal_count) for source in outcome.sources] == [
        (path, 1), ("data/runs/R-20261004-020000-triage/handoff/triage-P-0002.json", 0)]
    assert outcome.coverage.to_dict() == {} and outcome.stats["findings"] == 1


def test_read_sources_are_skipped_until_their_content_changes(tmp_path, conn):
    layout, current = probe(tmp_path, conn)
    handoff(layout, conn, RunStage.TRIAGE, "P-0001", [FINDING])
    save_state(conn, run(current, tmp_path))
    again = run(current, tmp_path)
    assert (again.status, again.skipped_reason) == (RunStatus.SKIPPED, "没有新的或变化的任务外发现来源")
    handoff(layout, conn, RunStage.TRIAGE, "P-0001", [FINDING, {**FINDING, "text": "第二条"}])
    rerun = run(current, tmp_path)
    assert len(rerun.signals) == 2


def test_broken_sources_are_retried(tmp_path, conn):
    layout, current = probe(tmp_path, conn)
    good = handoff(layout, conn, RunStage.TRIAGE, "P-0001", [FINDING])
    bad = handoff(layout, conn, RunStage.FIX, "0007", [])
    (layout.root / bad).write_text("{broken", encoding="utf-8")
    outcome = run(current, tmp_path)
    assert outcome.status is RunStatus.PARTIAL and [source.source_path for source in outcome.sources] == [good]
    assert outcome.notes == (f"来源读取失败，下次重试：{bad} 无法解析：JSONDecodeError",)


def test_archive_import(tmp_path, conn):
    _, current = probe(tmp_path, conn)
    archive = tmp_path / "archive"
    (archive / "2026-09").mkdir(parents=True)
    (archive / "2026-09" / "report.md").write_text(REPORT, encoding="utf-8")
    outcome = run(current, tmp_path, import_archive=archive)
    assert [signal.location for signal in outcome.signals] == [
        "src/Services/CalculateService.cs:CalculateService.SaveResult", "RefundService.cs:RefundService.Retry",
        "src/Detail.cs"]
    assert outcome.signals[0].release is None and outcome.signals[0].context["line"] == 210
    assert outcome.signals[0].occurred_at == datetime(2026, 9, 18, tzinfo=timezone.utc)
    assert outcome.stats["unlocated"] == 1
    assert "提取不到文件，需要手动补登：吞掉异常的 catch：OrderService.Page 没有位置" in outcome.notes
    save_state(conn, outcome)
    assert run(current, tmp_path, import_archive=archive).status is RunStatus.SKIPPED
    missing = run(current, tmp_path, import_archive=tmp_path / "none")
    assert missing.status is RunStatus.PARTIAL and missing.notes[0].startswith("来源读取失败")
