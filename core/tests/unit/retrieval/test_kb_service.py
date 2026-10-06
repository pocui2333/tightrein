from dataclasses import replace
from datetime import date, timedelta

import pytest
from knowledge_world import NOW, RUN, TOKYO, ReplayRig
from replay_support import record

from tightrein.domain.enums import ContextKind, KnowledgeStatus, KnowledgeType, KnowledgeWriteDecision, Stage
from tightrein.observability import events
from tightrein.observability.events import EventLog
from tightrein.observability.tracing import Tracer
from tightrein.retrieval import dedup
from tightrein.retrieval.context import ContextRequest
from tightrein.retrieval.dedup import WriteOrigin
from tightrein.retrieval.errors import EntryNotFound, IndexUnavailable, KnowledgeError
from tightrein.retrieval.models import SearchFilters
from tightrein.retrieval.service import KnowledgeService, RetrievalSettings, open_index
from tightrein.retrieval.writer import KnowledgeDraft
from tightrein.runner.task import Subject
from tightrein.store.db import connect
from tightrein.store.files import markdown
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import knowledge

ORIGIN = WriteOrigin(RUN, Stage.LEARN, Subject("week", "2026-10-05"), runner_override="replay")


def service(world, **options):
    return KnowledgeService(world.layout, world.conn, world.clock, world.tracer, zone=TOKYO, **options)


def kb_events(world):
    return [event for event in events.read(world.layout.events_log(NOW.date())) if event.agent == "kb"]


def draft(slug="new-lesson", title="分页从 1 开始", summary="分页参数从 1 开始", tags=("path:src/", "分页")):
    return KnowledgeDraft(KnowledgeType.DEFECT_PATTERN, slug, title, summary, tags, "正文。\n", date(2027, 4, 1))


def test_search_syncs_first_and_records_the_query(world):
    world.entry("DP-0001", "owner", "公司过滤", "列表接口按公司过滤")
    result = service(world).search("公司过滤", SearchFilters(types=("defect-pattern",)))
    assert ([hit.id for hit in result.hits], result.index_warnings) == (["DP-0001"], [])
    assert world.layout.knowledge_index().is_file()
    sync_event, search_event = kb_events(world)
    assert (sync_event.operation, sync_event.attributes["added"]) == ("run_script", 1)
    assert (search_event.operation, search_event.attributes) == ("execute_tool", {
        "operation": "search", "query": "公司过滤",
        "filters": {"types": ["defect-pattern"], "tags": [], "status": "active", "limit": 10}, "ids": ["DP-0001"]})


def test_a_broken_file_keeps_the_last_index_and_warns(world):
    path = world.entry("DP-0001", "owner", "公司过滤", "列表接口按公司过滤")
    service(world).search("公司过滤")
    path.write_text("---\nid: DP-0001\n---\n", encoding="utf-8")
    result = service(world).search("公司过滤")
    assert [hit.id for hit in result.hits] == ["DP-0001"]
    assert result.index_warnings == [
        "知识文件有格式错误，本次检索未包含最新改动；运行 tightrein admin kb sync 查看详情："
        "knowledge/defect-pattern/DP-0001-owner.md"]


def test_get_counts_hits_except_in_sandbox_or_when_asked_not_to(world):
    world.entry("DP-0001", "owner", "公司过滤", "列表接口按公司过滤")
    document = service(world).get("DP-0001")
    assert (document.id, document.frontmatter["summary"], document.index_warnings) == ("DP-0001", "列表接口按公司过滤", ())
    service(world).get("DP-0001", record_hit=False)
    sandbox_layout = WorkspaceLayout(world.layout.root, world.tool.root / "sandbox-output")
    world.entry("DP-0002", "later", "之后写的", "沙箱里看不到")
    sandboxed = KnowledgeService(sandbox_layout, world.conn, world.clock,
                                 Tracer(EventLog(sandbox_layout, world.redactor), world.clock, run_id=RUN),
                                 sandbox=True)
    sandboxed.get("DP-0001")
    with pytest.raises(EntryNotFound):
        sandboxed.get("DP-0002")
    record_row = knowledge.get(world.conn, "DP-0001")
    assert (record_row.hits, record_row.last_hit_at) == (1, NOW)
    assert (world.tool.root / "sandbox-output" / "events.jsonl").is_file()
    assert [event.attributes["recordHit"] for event in kb_events(world) if event.attributes["operation"] == "get"] == [
        True, False]


def test_get_resyncs_a_changed_file_and_reports_a_deleted_one(world):
    target = world.entry("DP-0001", "owner", "公司过滤", "列表接口按公司过滤")
    broken = world.entry("DP-0002", "broken", "坏文件", "之后改坏")
    service(world).sync()
    broken.write_text("没有 frontmatter\n", encoding="utf-8")
    world.entry("DP-0001", "owner", "公司与部门过滤", "改过的摘要")
    document = service(world).get("DP-0001")
    assert document.frontmatter["summary"] == "改过的摘要" and document.index_warnings != ()
    assert knowledge.get(world.conn, "DP-0001").summary == "改过的摘要"
    target.unlink()
    with pytest.raises(EntryNotFound):
        service(world).get("DP-0001")
    assert knowledge.get(world.conn, "DP-0001") is None


def test_related_stale_and_context_go_through_the_service(world):
    world.entry("DP-0001", "old", "旧", "旧条目", status="superseded", superseded_by="DP-0002")
    world.entry("DP-0002", "new", "新", "新条目", tags=("path:src/",), review_by="2026-10-01", updated="2026-09-20")
    world.entry("DP-0003", "idle", "闲置", "很久没用", updated="2026-08-01")
    kb = service(world, settings=replace(RetrievalSettings.default(), unused_days=30))
    assert [(entry.hit.id, entry.relation) for entry in kb.related("DP-0002").entries] == [("DP-0001", "supersedes")]
    report = kb.stale()
    assert ([hit.id for hit in report.overdue], [hit.id for hit in report.unused]) == (["DP-0002"], ["DP-0003"])
    bundle = kb.context_for(ContextRequest(ContextKind.STATIC_REVIEW, paths=("src/a.cs",)))
    assert [item.id for item in bundle.items] == ["DP-0002"]
    operations = [event.attributes["operation"] for event in kb_events(world)]
    assert operations == ["sync", "related", "stale", "context"]


def test_write_without_similar_entries_adds_directly(world):
    kb = service(world)
    outcome = kb.write(draft(), None, ORIGIN)
    assert (outcome.decision, outcome.written_id, outcome.reason) == (
        KnowledgeWriteDecision.ADD, "DP-0001", "没有相似条目，直接新增")
    assert knowledge.get(world.conn, "DP-0001").summary == "分页参数从 1 开始"
    assert "DP-0001 分页参数从 1 开始" in world.layout.knowledge_index(KnowledgeType.DEFECT_PATTERN).read_text(
        encoding="utf-8")
    write_event = kb_events(world)[-1]
    assert (write_event.decision, write_event.reason, write_event.attributes["writtenId"]) == (
        "add", "没有相似条目，直接新增", "DP-0001")


def test_write_with_similar_entries_asks_the_curator(world, repos, make_config, tmp_path):
    rig = ReplayRig(world, repos, make_config(), tmp_path / "recordings")
    world.entry("DP-0001", "paging", "分页从 0 开始", "分页参数从 0 开始", tags=("path:src/", "分页"))
    kb = service(world)
    kb.sync()
    new = draft()
    candidates = dedup.similar(world.conn, world.layout, kb.sources, new)
    record(rig.recordings, dedup.curator_task(world.layout, ORIGIN, new, candidates),
           {"decision": "add", "targetIds": [], "supersedes": ["DP-0001"], "reason": "结论相反"})
    outcome = kb.write(new, rig.runner(), ORIGIN)
    assert (outcome.written_id, outcome.superseded_ids) == ("DP-0002", ["DP-0001"])
    old = knowledge.get(world.conn, "DP-0001")
    assert (old.status, old.superseded_by) == (KnowledgeStatus.SUPERSEDED, "DP-0002")
    assert "DP-0001" not in world.layout.knowledge_index(KnowledgeType.DEFECT_PATTERN).read_text(encoding="utf-8")


def test_set_status_renews_and_archives(world):
    world.entry("DP-0001", "owner", "公司过滤", "列表接口按公司过滤")
    kb = service(world)
    kb.sync()
    kb.set_status("DP-0001", KnowledgeStatus.ACTIVE, review_by=date(2027, 10, 1))
    assert knowledge.get(world.conn, "DP-0001").review_by == date(2027, 10, 1)
    kb.set_status("DP-0001", KnowledgeStatus.ARCHIVED)
    path = world.layout.knowledge_file(KnowledgeType.DEFECT_PATTERN, "DP-0001", "owner")
    assert markdown.read(path).frontmatter["status"] == "archived"
    assert knowledge.get(world.conn, "DP-0001").status is KnowledgeStatus.ARCHIVED
    assert kb.search("公司过滤").hits == []


def test_sandbox_never_writes(world):
    kb = service(world, sandbox=True)
    assert kb.sync().changed is False and kb.regenerate_index_files() == []
    with pytest.raises(KnowledgeError, match="沙箱模式"):
        kb.write(draft(), None, ORIGIN)


def test_settings_come_from_project_thresholds(make_config):
    config = make_config(thresholds={
        "suppressionDays": {"value": 30, "min": 7, "max": 90},
        "triage": {"deferredReopenOccurrences": {"value": 3, "min": 1, "max": 10}},
        "retrieval": {"unusedDays": {"value": 60, "min": 30, "max": 180},
                      "contextLimits": {"fix": {"value": 8, "min": 1, "max": 30}}},
    })
    settings = RetrievalSettings.from_config(config)
    assert (settings.unused_days, settings.inline_full_tokens) == (60, 3000)
    assert settings.context_limits == {ContextKind.STATIC_REVIEW: 20, ContextKind.TRIAGE: 15, ContextKind.FIX: 8}


def test_open_index_requires_an_initialized_database(world, tmp_path):
    missing = WorkspaceLayout(tmp_path / "none")
    with pytest.raises(IndexUnavailable, match="不存在"):
        open_index(missing)
    blank = WorkspaceLayout(tmp_path / "blank")
    connect(blank.database()).close()
    with pytest.raises(IndexUnavailable, match="迁移未执行"):
        open_index(blank)
    open_index(world.layout).close()


def test_the_clock_decides_overdue_in_the_local_zone(world):
    world.entry("DP-0001", "owner", "公司过滤", "列表接口按公司过滤", review_by="2026-10-04")
    world.clock.advance(timedelta(hours=-3, minutes=-1))
    assert [hit.id for hit in service(world).stale().overdue] == ["DP-0001"]
