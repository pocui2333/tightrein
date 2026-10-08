from tightrein.collect.common.source import SourceStatus
from tightrein.collect.incidental import source
from tightrein.store.tables import state


def runtime(source_runtime):
    return source_runtime(modules={source.SOURCE: {"status": "enabled"}})


def save(current, result):
    for key, value in result.state.items():
        state.put(current.conn, key, value, current.clock)


def test_signals_and_read_records(source_runtime, write_handoff, finding):
    current = runtime(source_runtime)
    path = write_handoff(current.workspace, "assess.triage", "P-0001", [finding, {**finding, "category": "naming"}])
    write_handoff(current.workspace, "assess.triage", "P-0002", [])
    result = source.collect(current)
    assert result.status is SourceStatus.DONE and len(result.signals) == 1 and result.coverage == []
    [signal] = result.signals
    assert (signal.source, signal.check_type, signal.location, signal.commit) == (
        "collect.incidental", "incidental:defect", "src/Services/RefundService.cs:88", "c" * 40)
    assert signal.group_key == "incidental:src/Services/RefundService.cs:RefundService.Retry:defect"
    relative = path.relative_to(current.workspace.root).as_posix()
    assert relative in result.state[source.READ_KEY] and len(result.state[source.READ_KEY]) == 2
    assert result.metrics.produced == {"sources": 2, "findings": 2, "dropped": 1, "signals": 1}
    assert result.notes == ["1 条发现的类别不在缺陷、安全、性能、数据之内，已丢弃"]


def test_read_sources_are_skipped_until_their_content_changes(source_runtime, write_handoff, finding):
    current = runtime(source_runtime)
    write_handoff(current.workspace, "assess.triage", "P-0001", [finding])
    save(current, source.collect(current))
    again = source.collect(current)
    assert (again.status, again.reason) == (SourceStatus.SKIPPED, source.NOTHING_NEW)
    write_handoff(current.workspace, "assess.triage", "P-0001", [finding, {**finding, "text": "第二条"}])
    assert len(source.collect(current).signals) == 2


def test_broken_sources_are_retried(source_runtime, write_handoff, finding):
    current = runtime(source_runtime)
    good = write_handoff(current.workspace, "assess.triage", "P-0001", [finding])
    bad = write_handoff(current.workspace, "implement.code", "0007", [])
    bad.write_text("{broken", encoding="utf-8")
    result = source.collect(current)
    relative = bad.relative_to(current.workspace.root).as_posix()
    assert result.status is SourceStatus.PARTIAL
    assert list(result.state[source.READ_KEY]) == [good.relative_to(current.workspace.root).as_posix()]
    assert result.reason == f"跳过，下次重试：{relative} 无法解析：JSONDecodeError"
    good.unlink()
    assert source.collect(current).status is SourceStatus.FAILED
