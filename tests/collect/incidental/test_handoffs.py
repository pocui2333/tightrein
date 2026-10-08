from datetime import UTC, datetime

from tightrein.collect.incidental import handoffs
from tightrein.protocol.handoff import Status
from tightrein.store.files.layout import WorkspaceLayout

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
POINTS = {"assess.triage": "commit", "implement": "baseCommit"}


def test_point_of_a_handoff_file():
    assert handoffs.point_of("21-assess.triage-handoff.json") == "assess.triage"
    assert handoffs.point_of("35-implement.code.r2-handoff.json") == "implement.code"


def test_handoff_candidates(tmp_path, write_handoff, finding):
    layout = WorkspaceLayout(tmp_path)
    triage = write_handoff(layout, "assess.triage", "P-0001", [finding])
    fix = write_handoff(layout, "implement.code", "0007", [{**finding, "file": "src/A.cs", "line": None,
                                                           "symbol": None, "text": "A 类缺少空值判断"}], round=2)
    write_handoff(layout, "assess.issue", "P-0002", [finding])
    write_handoff(layout, "release.pr", "0008", [finding])
    assert [path for path, _ in handoffs.candidates(layout, POINTS)] == [triage, fix]
    found = dict(handoffs.unread(layout, POINTS, {}, NOW))
    first = found[triage.relative_to(tmp_path).as_posix()].findings[0]
    assert (first.file, first.line, first.symbol, first.commit, first.point, first.subject) == (
        "src/Services/RefundService.cs", 88, "RefundService.Retry", "c" * 40, "assess.triage", "P-0001")
    assert first.occurred_at == datetime(2026, 10, 4, 2, 30, tzinfo=UTC)
    second = found[fix.relative_to(tmp_path).as_posix()].findings[0]
    assert second.commit == "f" * 40 and second.line is None


def test_failed_handoffs_still_give_findings_and_known_ones_are_skipped(tmp_path, write_handoff, finding):
    layout = WorkspaceLayout(tmp_path)
    path = write_handoff(layout, "assess.triage", "P-0001", [finding], status=Status.FAILED)
    [(relative, read)] = list(handoffs.unread(layout, POINTS, {}, NOW))
    assert len(read.findings) == 1 and read.error is None
    assert list(handoffs.unread(layout, POINTS, {relative: read.content_hash}, NOW)) == [(relative, None)]
    path.write_text("{broken", encoding="utf-8")
    [(_, broken)] = list(handoffs.unread(layout, POINTS, {relative: read.content_hash}, NOW))
    assert broken.error == f"{relative} 无法解析：JSONDecodeError"


def test_malformed_findings_fail_the_whole_file(tmp_path, write_handoff, finding):
    layout = WorkspaceLayout(tmp_path)
    write_handoff(layout, "assess.triage", "P-0001", [{**finding, "confidence": "maybe"}])
    [(_, read)] = list(handoffs.unread(layout, POINTS, {}, NOW))
    assert read.error is not None and "不合格式" in read.error
