from datetime import timedelta

from learn_world import learn_env, make_learn_world
from pipeline_world import NOW

from tightrein.domain.enums import KnowledgeType, RunnerStatus
from tightrein.pipeline.learn.steps import curate
from tightrein.store.files import markdown
from tightrein.store.files.markdown import MarkdownDocument
from tightrein.store.repos import knowledge


def entry(world, entry_id, tags=("topic", "orders"), review_by="2027-03-01", status="active", updated="2026-09-20"):
    kind = KnowledgeType.from_prefix(entry_id.split("-")[0])
    frontmatter = {"id": entry_id, "type": kind.value, "summary": f"经验 {entry_id}", "tags": list(tags),
                   "status": status, "supersededBy": None, "updated": updated, "reviewBy": review_by, "related": []}
    markdown.write(world.layout.knowledge_file(kind, entry_id, "entry"),
                   MarkdownDocument(frontmatter, f"# 经验 {entry_id}\n\n正文 {entry_id}\n"))


def compared(contradictions=(), duplicates=()):
    return {"mode": "compare", "draft": None, "contradictions": list(contradictions), "duplicates": list(duplicates)}


def subjects(drafts):
    return [(item.subject, item.evidence["reason"]) for item in drafts]


def test_other_types_get_one_review_suggestion_each(tmp_path):
    world = make_learn_world(tmp_path)
    entry(world, "DP-0001", tags=("a",), review_by="2026-09-01")
    entry(world, "TO-0001", tags=("b",), updated="2026-05-01")
    entry(world, "TL-0001", tags=("c",), review_by="2026-09-01")
    entry(world, "CT-0002", tags=("d",), review_by="2026-09-01", status="archived")
    world.knowledge_service().sync()
    knowledge.record_hit(world.conn, "DP-0001", NOW - timedelta(days=1))
    knowledge.record_hit(world.conn, "TL-0001", NOW - timedelta(days=1))
    drafts, errors = curate.review_drafts(learn_env(world))
    assert subjects(drafts) == [("overdue:DP-0001", "已过复核日期"), ("unused:TO-0001", "长期没有被命中")]
    assert errors == []


def test_overdue_lessons_are_archived_when_unused_and_renewed_when_still_hit(tmp_path):
    world = make_learn_world(tmp_path)
    entry(world, "TL-0001", tags=("a",), review_by="2026-09-01")
    entry(world, "FL-0001", tags=("b",), review_by="2026-09-01", updated="2026-05-01")
    entry(world, "FL-0002", tags=("c",), updated="2026-05-01")
    entry(world, "DP-0001", tags=("d",), review_by="2026-09-01", updated="2026-05-01")
    world.knowledge_service().sync()
    knowledge.record_hit(world.conn, "TL-0001", NOW - timedelta(days=1))
    env = learn_env(world)
    assert curate.cleanup(env) == [{"id": "FL-0001", "action": "archived"}, {"id": "TL-0001", "action": "renewed"}]
    statuses = {record.id: (record.status.value, record.review_by) for record in knowledge.find(world.conn)}
    review_by = NOW.date() + timedelta(days=world.config.whole_threshold("learn.lessonReviewDays"))
    assert statuses["TL-0001"] == ("active", review_by)
    assert statuses["FL-0001"][0] == "archived" and statuses["FL-0002"][0] == "active"
    assert statuses["DP-0001"][0] == "active"


def test_groups_are_compared_and_findings_become_suggestions(tmp_path):
    world = make_learn_world(tmp_path)
    for entry_id in ("TL-0001", "TL-0002", "TL-0003"):
        entry(world, entry_id)
    world.knowledge_service().sync()
    for entry_id in ("TL-0001", "TL-0002", "TL-0003"):
        knowledge.record_hit(world.conn, entry_id, NOW)
    world.runner.add("lesson-writer", compared(
        contradictions=[{"ids": ["TL-0001", "TL-0002"], "facts": "对超时的判断相反"}],
        duplicates=[{"ids": ["TL-0002", "TL-0003"], "reason": "同一规律"}, {"ids": ["TL-0003", "TL-0009"], "reason": "x"}]))
    drafts, errors = curate.review_drafts(learn_env(world))
    assert subjects(drafts) == [("contradiction:TL-0001,TL-0002", "对超时的判断相反"),
                                ("duplicate:TL-0002,TL-0003", "同一规律")]
    assert drafts[0].evidence["compared"] is True
    task = world.runner.tasks[0]
    assert task.role == "lesson-writer-compare-1" and "正文 TL-0003" in task.instructions.prompt
    assert errors == []


def test_a_failed_comparison_is_reported_without_suggestions(tmp_path):
    world = make_learn_world(tmp_path)
    entry(world, "TL-0001")
    entry(world, "TL-0002")
    world.knowledge_service().sync()
    for entry_id in ("TL-0001", "TL-0002"):
        knowledge.record_hit(world.conn, entry_id, NOW)
    world.runner.add("lesson-writer", RunnerStatus.FAILED)
    drafts, errors = curate.review_drafts(learn_env(world))
    assert drafts == []
    assert errors == [{"item": "compare:TL-0001,TL-0002", "reason": "比对没有结果：failed fake-error"}]
