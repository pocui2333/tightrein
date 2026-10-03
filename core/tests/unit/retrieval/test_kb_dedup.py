import os
from datetime import date

import pytest
from knowledge_world import RUN, ReplayRig
from replay_support import record

from tightrein.domain.enums import KnowledgeType, KnowledgeWriteDecision, RunnerStatus, Stage, YieldOutcome
from tightrein.retrieval import dedup
from tightrein.retrieval.dedup import Decision, WriteOrigin
from tightrein.retrieval.errors import DraftInvalid, WriteDecisionInvalid
from tightrein.retrieval.indexers import FtsIndexer
from tightrein.retrieval.locations import routes_of
from tightrein.retrieval.models import EntryDocument
from tightrein.retrieval.ranking import FtsSource
from tightrein.retrieval.sync import Synchronizer
from tightrein.retrieval.writer import Content, KnowledgeDraft, KnowledgeWriter, check_draft
from tightrein.runner.result import RunnerResult
from tightrein.runner.task import Subject
from tightrein.store.files import markdown
from tightrein.store.repos import stage_yield

ORIGIN = WriteOrigin(RUN, Stage.LEARN, Subject("week", "2026-10-05"), runner_override="replay")
DRAFT = KnowledgeDraft(KnowledgeType.DEFECT_PATTERN, "company-filter", "列表查询缺少公司过滤",
                       "列表接口没有按公司过滤", ("path:src/Services/", "权限"), "列表接口直接返回全部公司的数据。\n",
                       date(2027, 4, 1), related=("TO-0001",), source_run_id=RUN)
MERGED = {"title": "查询接口缺少公司过滤", "summary": "查询与列表接口都没有按公司过滤",
          "tags": ["path:src/Services/", "权限", "公司"], "body": "合并后的正文。"}


@pytest.fixture
def rig(world, repos, make_config, tmp_path):
    return ReplayRig(world, repos, make_config(), tmp_path / "recordings")


def seed(world):
    world.entry("DP-0001", "list-filter", "列表缺少公司过滤", "列表接口没有按公司过滤", tags=("path:src/Services/", "权限"),
                related=("TO-0001",))
    world.entry("DP-0002", "query-filter", "查询缺少公司过滤", "查询接口没有按公司过滤", tags=("path:src/Services/", "公司"),
                related=("FL-0001",))
    world.entry("TO-0001", "soft-delete", "软删除", "软删除是已接受的取舍")
    world.entry("FL-0001", "lesson", "公司过滤的修复", "修复经验")
    world.entry("TL-0001", "other-type", "公司过滤", "列表接口没有按公司过滤")
    synchronizer = Synchronizer(world.layout, world.conn, world.clock, [FtsIndexer()], routes_of(world.conn))
    assert synchronizer.sync().errors == []


def candidates(world):
    return dedup.similar(world.conn, world.layout, [FtsSource(world.conn)], DRAFT)


def decide(world, rig, *outputs):
    found = candidates(world)
    feedback = []
    for attempt, output in enumerate(outputs, start=1):
        task = dedup.curator_task(world.layout, ORIGIN, DRAFT, found, attempt, feedback)
        record(rig.recordings, task, output)
        if isinstance(output, dict):
            feedback = dedup.check_decision(Decision.from_output(output), [document.id for document in found])
    return dedup.decide(rig.runner(), world.clock, world.layout, ORIGIN, DRAFT, found)


def frontmatter(world, kind, entry_id, slug):
    return markdown.read(world.layout.knowledge_file(kind, entry_id, slug)).frontmatter


def test_draft_checks_list_every_problem(world):
    draft = KnowledgeDraft(KnowledgeType.DEFECT_PATTERN, "Bad Slug", " ", "摘要", ("route:get /x",), "正文",
                           date(2027, 1, 1), related=("DP-0099",))
    with pytest.raises(DraftInvalid) as raised:
        check_draft(world.conn, draft)
    assert str(raised.value) == (
        "草稿不合格：title 不能为空；简称只能由小写字母、数字与连字符组成：'Bad Slug'；"
        "tags[0]：route: 须为「HTTP 方法 空格 以 / 开头的路由模板」，例如 route:POST /api/Material/Query；"
        "related 中的条目 DP-0099 不存在")


def test_similar_entries_have_the_same_type_and_are_active(world):
    seed(world)
    assert [document.id for document in candidates(world)] == ["DP-0001", "DP-0002"]
    task = dedup.curator_task(world.layout, ORIGIN, DRAFT, candidates(world))
    assert (task.role, task.output_schema, task.access.value, task.workdir, task.allowed_commands) == (
        "knowledge-curator", "runner/roles/knowledge-curator.schema.json", "read-only", world.layout.knowledge_dir(), ())
    assert task.instructions.skills[0].name == "learn"
    assert task.instructions.skills[0].references == ("references/knowledge-curator.md",)
    prompt = task.instructions.prompt
    assert "## 草稿" in prompt and "标题：列表查询缺少公司过滤" in prompt
    assert "### DP-0001(knowledge/defect-pattern/DP-0001-list-filter.md)" in prompt and "# 查询缺少公司过滤" in prompt


def test_add_supersedes_old_entries(world, rig):
    seed(world)
    decision = decide(world, rig, {"decision": "add", "targetIds": [], "supersedes": ["DP-0001"],
                                   "reason": "新条目推翻了旧结论"})
    outcome = dedup.apply(KnowledgeWriter(world.layout, world.conn, world.clock), DRAFT, decision)
    assert (outcome.decision, outcome.written_id, outcome.superseded_ids) == (
        KnowledgeWriteDecision.ADD, "DP-0003", ["DP-0001"])
    created = markdown.read(world.layout.knowledge_file(KnowledgeType.DEFECT_PATTERN, "DP-0003", "company-filter"))
    assert created.frontmatter == {
        "id": "DP-0003", "type": "defect-pattern", "summary": "列表接口没有按公司过滤",
        "tags": ["path:src/Services/", "权限"], "status": "active", "supersededBy": None, "updated": "2026-10-05",
        "reviewBy": "2027-04-01", "related": ["TO-0001"], "sourceRunId": RUN}
    assert created.body == "# 列表查询缺少公司过滤\n\n列表接口直接返回全部公司的数据。\n"
    old = frontmatter(world, KnowledgeType.DEFECT_PATTERN, "DP-0001", "list-filter")
    assert (old["status"], old["supersededBy"], old["updated"]) == ("superseded", "DP-0003", "2026-10-05")


def test_update_rewrites_the_target_in_place(world, rig):
    seed(world)
    decision = decide(world, rig, {"decision": "update", "targetIds": ["DP-0001"], "supersedes": [],
                                   "result": MERGED, "reason": "同一个问题，补充内容"})
    outcome = dedup.apply(KnowledgeWriter(world.layout, world.conn, world.clock), DRAFT, decision)
    assert (outcome.written_id, outcome.superseded_ids) == ("DP-0001", [])
    document = markdown.read(world.layout.knowledge_file(KnowledgeType.DEFECT_PATTERN, "DP-0001", "list-filter"))
    assert (document.frontmatter["summary"], document.frontmatter["tags"], document.frontmatter["related"]) == (
        "查询与列表接口都没有按公司过滤", ["path:src/Services/", "权限", "公司"], ["TO-0001"])
    assert document.body == "# 查询接口缺少公司过滤\n\n合并后的正文。\n"


def test_merge_writes_a_new_entry_and_supersedes_the_targets(world, rig):
    seed(world)
    decision = decide(world, rig, {"decision": "merge", "targetIds": ["DP-0001", "DP-0002"], "supersedes": [],
                                   "result": MERGED, "reason": "两条讲的是同一件事"})
    outcome = dedup.apply(KnowledgeWriter(world.layout, world.conn, world.clock), DRAFT, decision)
    assert (outcome.written_id, outcome.superseded_ids) == ("DP-0003", ["DP-0001", "DP-0002"])
    merged = frontmatter(world, KnowledgeType.DEFECT_PATTERN, "DP-0003", "company-filter")
    assert (merged["summary"], merged["related"]) == ("查询与列表接口都没有按公司过滤", ["TO-0001", "FL-0001"])
    for entry_id, slug in (("DP-0001", "list-filter"), ("DP-0002", "query-filter")):
        assert frontmatter(world, KnowledgeType.DEFECT_PATTERN, entry_id, slug)["supersededBy"] == "DP-0003"


def test_noop_writes_nothing(world, rig):
    seed(world)
    decision = decide(world, rig, {"decision": "noop", "targetIds": ["DP-0001"], "supersedes": [],
                                   "reason": "已有等价条目"})
    before = sorted(world.layout.knowledge_dir().rglob("*.md"))
    outcome = dedup.apply(KnowledgeWriter(world.layout, world.conn, world.clock), DRAFT, decision)
    assert (outcome.decision, outcome.written_id, outcome.paths) == (KnowledgeWriteDecision.NOOP, None, [])
    assert sorted(world.layout.knowledge_dir().rglob("*.md")) == before


def test_a_semantically_invalid_decision_is_retried_once(world, rig):
    seed(world)
    invalid = {"decision": "update", "targetIds": ["DP-0001", "DP-0009"], "supersedes": [], "result": MERGED,
               "reason": "改两条"}
    valid = {"decision": "noop", "targetIds": ["DP-0002"], "supersedes": [], "reason": "已有等价条目"}
    decision = decide(world, rig, invalid, valid)
    assert (decision.decision, decision.target_ids) == (KnowledgeWriteDecision.NOOP, ("DP-0002",))
    retry = dedup.curator_task(world.layout, ORIGIN, DRAFT, candidates(world), 2,
                               dedup.check_decision(Decision.from_output(invalid), ["DP-0001", "DP-0002"]))
    assert retry.instructions.prompt.endswith(
        "## 上一次判断的问题\n\n- DP-0009 不在候选条目中，只能引用 DP-0001、DP-0002\n\n- update 的 targetIds 须恰好 1 个\n")


def test_each_judgement_call_records_whether_it_avoided_a_duplicate(world):
    invalid = {"decision": "update", "targetIds": ["DP-0009"], "supersedes": [], "result": MERGED, "reason": "改"}
    valid = {"decision": "noop", "targetIds": ["DP-0002"], "supersedes": [], "reason": "已有等价条目"}

    class Curator:
        outputs = [invalid, valid]

        def run(self, task, **_):
            return RunnerResult(RunnerStatus.OK, "fake", output=self.outputs.pop(0), attempts=1)

    found = [EntryDocument("DP-0002", {}, "查询接口没有按公司过滤", "knowledge/defect-pattern/DP-0002-query-filter.md")]
    dedup.decide(Curator(), world.clock, world.layout, ORIGIN, DRAFT, found, world.conn)
    rows = stage_yield.find(world.conn)
    assert [(row.stage, row.role, row.attempt, row.outcome) for row in rows] == [
        (Stage.LEARN, "knowledge-curator", 1, YieldOutcome.NO_YIELD),
        (Stage.LEARN, "knowledge-curator", 2, YieldOutcome.USEFUL)]
    assert all(row.decided_at is not None for row in rows)


def test_a_decision_invalid_twice_is_not_written(world, rig):
    seed(world)
    invalid = {"decision": "add", "targetIds": ["DP-0001"], "supersedes": [], "reason": "新增"}
    with pytest.raises(WriteDecisionInvalid) as raised:
        decide(world, rig, invalid, invalid)
    assert raised.value.reasons == ("add 不针对已有条目，targetIds 须为空；被推翻的旧条目写在 supersedes 中",)


def test_a_runner_without_a_decision_fails_the_write(world, rig):
    seed(world)
    found = candidates(world)
    record(rig.recordings, dedup.curator_task(world.layout, ORIGIN, DRAFT, found), None, "failed", "tool-error")
    with pytest.raises(WriteDecisionInvalid, match="执行器没有给出判断：failed tool-error"):
        dedup.decide(rig.runner(), world.clock, world.layout, ORIGIN, DRAFT, found)


def test_a_failed_write_leaves_no_partial_file(world, monkeypatch):
    seed(world)
    writer = KnowledgeWriter(world.layout, world.conn, world.clock)

    def broken(source, target):
        raise OSError("磁盘已满")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        writer.update("DP-0001", Content("新标题", "新摘要", ("x",), "新正文"))
    directory = world.layout.knowledge_type_dir(KnowledgeType.DEFECT_PATTERN)
    assert sorted(path.name for path in directory.iterdir()) == ["DP-0001-list-filter.md", "DP-0002-query-filter.md"]
    assert frontmatter(world, KnowledgeType.DEFECT_PATTERN, "DP-0001", "list-filter")["summary"] == "列表接口没有按公司过滤"
