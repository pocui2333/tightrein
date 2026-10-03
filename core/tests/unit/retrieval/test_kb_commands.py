from datetime import date, timedelta

import pytest
from knowledge_world import NOW

from tightrein.domain.enums import KnowledgeType
from tightrein.observability import events
from tightrein.retrieval import commands
from tightrein.retrieval.errors import IndexUnavailable, SyncFailed

TRACE = "0af7651916cd43dd8448eb211c80319c"
PARENT = "b7ad6b7169203331"


def environ(world, **extra):
    return {"TIGHTREIN_RUN_ID": "R-20261005-030000-triage", "TIGHTREIN_TRACE_ID": TRACE,
            "TIGHTREIN_PARENT_SPAN_ID": PARENT, **extra}


def opened(world, **extra):
    return commands.open_service(world.layout.root, environ(world, **extra), world.clock)


def seed(world):
    world.entry("DP-0001", "owner", "公司过滤", "列表接口按公司过滤", tags=("path:src/",), related=("TO-0001",))
    world.entry("TO-0001", "soft-delete", "软删除", "软删除是取舍")


def test_the_workspace_comes_from_the_argument_or_the_environment(tmp_path):
    assert commands.workspace_from({"TIGHTREIN_WORKSPACE": "/w/sample"}, tmp_path) == tmp_path
    assert str(commands.workspace_from({"TIGHTREIN_WORKSPACE": "/w/sample"})) == "/w/sample"
    with pytest.raises(IndexUnavailable, match="TIGHTREIN_WORKSPACE"):
        commands.workspace_from({})


def test_command_results_are_plain_json(world):
    seed(world)
    service = opened(world)
    found = commands.search(service, "公司过滤", types=("defect-pattern",))
    assert [hit["id"] for hit in found["hits"]] == ["DP-0001"] and found["indexWarnings"] == []
    assert set(found["hits"][0]) == {"id", "type", "summary", "path", "score"}
    document = commands.get(service, "DP-0001")
    assert (document["id"], document["frontmatter"]["related"], document["body"].splitlines()[0]) == (
        "DP-0001", ["TO-0001"], "# 公司过滤")
    assert commands.related(service, "TO-0001")["entries"] == [{
        "id": "DP-0001", "type": "defect-pattern", "summary": "列表接口按公司过滤",
        "path": "knowledge/defect-pattern/DP-0001-owner.md", "score": 0.0, "relation": "related-by",
        "status": "active"}]
    assert commands.stale(service) == {"overdue": [], "unused": [], "contradictionGroups": [], "indexWarnings": []}
    service.conn.close()
    logged = events.read(world.layout.events_log(NOW.date()))
    assert {(event.trace_id, event.parent_span_id, event.run_id) for event in logged} == {
        (TRACE, PARENT, "R-20261005-030000-triage")}


def test_sync_lists_errors_as_a_failure(world):
    seed(world)
    service = opened(world)
    assert commands.sync(service) == {"added": ["DP-0001", "TO-0001"], "updated": [], "removed": []}
    world.layout.knowledge_file(KnowledgeType.TRADEOFF, "TO-0001", "soft-delete").write_text("坏了\n", encoding="utf-8")
    with pytest.raises(SyncFailed) as raised:
        commands.sync(service, full=True)
    assert [issue.path for issue in raised.value.issues] == ["knowledge/tradeoff/TO-0001-soft-delete.md"]


def test_sandbox_reads_without_hits_and_logs_into_the_output_dir(world, tmp_path):
    seed(world)
    opened(world).conn.close()
    commands.sync(opened(world))
    output = tmp_path / "sandbox"
    with pytest.raises(IndexUnavailable, match="TIGHTREIN_OUTPUT_DIR"):
        opened(world, TIGHTREIN_SANDBOX="1")
    service = opened(world, TIGHTREIN_SANDBOX="1", TIGHTREIN_OUTPUT_DIR=str(output))
    commands.get(service, "DP-0001")
    assert [event.attributes["operation"] for event in events.read(output / "events.jsonl")] == ["get"]
    assert service.conn.execute("SELECT hits FROM knowledge_meta WHERE id = 'DP-0001'").fetchone()[0] == 0


def test_an_uninitialized_workspace_is_unavailable(tmp_path, world):
    with pytest.raises(IndexUnavailable):
        commands.open_service(tmp_path / "missing", {}, world.clock)


def test_queries_collect_search_events_since_a_day(world):
    seed(world)
    service = opened(world)
    commands.search(service, "公司过滤")
    world.clock.advance(timedelta(days=1))
    commands.search(service, "软删除", status="any")
    commands.get(service, "DP-0001")
    found = [item.to_dict() for item in commands.queries(world.layout, date(2026, 10, 6))]
    assert found == [{"timestamp": "2026-10-06T03:00:00Z", "runId": "R-20261005-030000-triage", "query": "软删除",
                      "filters": {"types": [], "tags": [], "status": "any", "limit": 10}, "ids": ["TO-0001"]}]
    assert [item.query for item in commands.queries(world.layout, date(2026, 10, 1))] == ["公司过滤", "软删除"]
