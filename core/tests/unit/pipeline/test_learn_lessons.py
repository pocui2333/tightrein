from datetime import timedelta

from learn_world import (
    issue,
    learn_env,
    lesson_output,
    make_learn_world,
    problem_with,
    triaged,
)
from pipeline_world import NOW

from tightrein.domain.enums import (
    Disposition,
    KnowledgeType,
    RunnerStatus,
    TriageOutcome,
    Verdict,
)
from tightrein.pipeline.learn.steps import lessons
from tightrein.store import idempotency
from tightrein.store.repos import knowledge, pulls
from tightrein.store.repos.pulls import PullRecord


def entries(world, kind):
    return [record.id for record in knowledge.find(world.conn, types=(kind.value,))]


def test_a_misjudged_triage_becomes_a_lesson_once(tmp_path):
    world = make_learn_world(tmp_path)
    problem_with(world, "P-0001", title="GET /api/Order 超时")
    triaged(world, "P-0001", outcome=TriageOutcome.FALSE_CONFIRM, outcome_at=NOW)
    world.runner.add("lesson-writer", lesson_output())
    report = lessons.write_lessons(learn_env(world))
    assert [item.to_dict() for item in report.results] == [
        {"source": "lesson:triage:P-0001:1", "knowledgeId": "TL-0001", "decision": "add"}]
    assert entries(world, KnowledgeType.TRIAGE_LESSON) == ["TL-0001"]
    task = world.runner.tasks[0]
    assert (task.role, task.subject_id) == ("lesson-writer", "P-0001")
    assert "实际结果：误判为成立" in task.instructions.prompt
    assert lessons.write_lessons(learn_env(world)).results == []
    assert len(world.runner.tasks) == 1


def test_an_empty_draft_is_marked_without_writing(tmp_path):
    world = make_learn_world(tmp_path)
    problem_with(world, "P-0001")
    triaged(world, "P-0001", attempt=2, outcome=TriageOutcome.OVERRIDDEN, outcome_at=NOW)
    triaged(world, "P-0001", verdict=Verdict.REFUTED, disposition=Disposition.FALSE_POSITIVE)
    world.runner.add("lesson-writer", lesson_output(draft=False))
    report = lessons.write_lessons(learn_env(world))
    assert [item.knowledge_id for item in report.results] == [None]
    assert idempotency.get(world.conn, "lesson:triage:P-0001:2").result == {"knowledgeId": None, "decision": None}
    assert entries(world, KnowledgeType.TRIAGE_LESSON) == []


def test_failures_retry_and_give_up_after_the_limit(tmp_path):
    world = make_learn_world(tmp_path)
    problem_with(world, "P-0001")
    triaged(world, "P-0001", outcome=TriageOutcome.FALSE_CONFIRM, outcome_at=NOW)
    world.runner.add("lesson-writer", RunnerStatus.FAILED)
    for _ in range(2):
        report = lessons.write_lessons(learn_env(world))
        assert report.errors and report.attention == []
    assert idempotency.get(world.conn, "lesson:triage:P-0001:1") is None
    report = lessons.write_lessons(learn_env(world))
    assert [(item.kind, item.subject_id) for item in report.attention] == [("lesson-failed", "lesson:triage:P-0001:1")]
    assert report.attention[0].command == "在 knowledge/triage-lesson/ 下手写一条经验"
    assert lessons.write_lessons(learn_env(world)).errors == []


def test_requested_changes_of_one_pull_request_share_a_call_and_carry_the_source(tmp_path):
    world = make_learn_world(tmp_path)
    issue(world, "0007")
    reviews = [{"id": "R1", "author": {"login": "lead"}, "state": "CHANGES_REQUESTED", "body": "错误处理请统一用已有的包装"},
               {"id": "R2", "author": {"login": "lead"}, "state": "APPROVED", "body": ""},
               {"id": "R3", "author": {"login": "peer"}, "state": "CHANGES_REQUESTED", "body": "命名请用完整单词"}]
    pulls.save(world.conn, PullRecord("0007", 12, "u", "b", "修复订单查询", "MERGED", NOW, reviews=reviews))
    world.runner.add("lesson-writer", lesson_output("fix-lesson", "error-wrapping", "错误处理用已有的包装"))
    report = lessons.write_lessons(learn_env(world))
    assert [item.source for item in report.results] == ["lesson:pr:0007:R1", "lesson:pr:0007:R3"]
    assert entries(world, KnowledgeType.FIX_LESSON) == ["FL-0001"]
    assert "命名请用完整单词" in world.runner.tasks[0].instructions.prompt
    (record,) = knowledge.find(world.conn, types=(KnowledgeType.FIX_LESSON.value,))
    assert record.review_by == NOW.date() + timedelta(days=world.config.whole_threshold("learn.lessonReviewDays"))
    body = (world.layout.root / record.path).read_text(encoding="utf-8")
    assert "## 来源\n\n- 类型：评审驳回\n- 对象：issue 0007\n" in body


def test_approvals_and_plain_comments_are_not_lessons(tmp_path):
    world = make_learn_world(tmp_path)
    issue(world, "0007")
    reviews = [{"id": "R1", "author": {"login": "lead"}, "state": "COMMENTED", "body": "命名请用完整单词"},
               {"id": "R2", "author": {"login": "lead"}, "state": "APPROVED", "body": "可以"}]
    pulls.save(world.conn, PullRecord("0007", 12, "u", "b", "修复订单查询", "MERGED", NOW, reviews=reviews))
    assert lessons.write_lessons(learn_env(world)).results == []
    assert world.runner.tasks == []


def test_output_mode_writes_no_lessons(tmp_path):
    world = make_learn_world(tmp_path)
    problem_with(world, "P-0001")
    triaged(world, "P-0001", outcome=TriageOutcome.FALSE_CONFIRM, outcome_at=NOW)
    assert lessons.write_lessons(learn_env(world, tmp_path / "out")).results == []
    assert world.runner.tasks == []
