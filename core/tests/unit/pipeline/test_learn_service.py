from datetime import timedelta

from learn_world import WEEK, ZONE, closed_fixed, issue, make_learn_world, problem_with, triaged
from pipeline_world import NOW

from tightrein.domain.enums import Stage, TriageOutcome
from tightrein.observability.events import EventLog
from tightrein.observability.redact import Redactor
from tightrein.pipeline.learn.service import LearnDeps, LearnService
from tightrein.pipeline.learn.steps import metrics
from tightrein.store.files import documents, handoff_files
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import metric_snapshots, runs, stage_yield


class Notifications:
    def __init__(self):
        self.sent = []

    def notify(self, event_type, subject_id, text, title="tightrein"):
        self.sent.append((event_type, subject_id, text))


def make_service(world, output_dir=None, notifier=None):
    layout = world.layout if output_dir is None else WorkspaceLayout(world.layout.root, output_dir)
    deps = LearnDeps(layout, world.tool, world.config, world.conn, world.clock, EventLog(layout, Redactor()),
                     world.runner, world.knowledge_service(), notifier, pid_alive=lambda pid: True, zone=ZONE)
    return LearnService(deps)


def test_report_writes_a_result_document_and_snapshots(tmp_path):
    world = make_learn_world(tmp_path)
    issue(world, "0007")
    closed_fixed(world, "0007")
    notifications = Notifications()
    result = make_service(world, notifier=notifications).report()
    assert result.report == world.layout.weekly_report(WEEK)
    text = result.report.read_text(encoding="utf-8")
    assert documents.check(text) == []
    document = documents.read(result.report)
    assert (document.header["kind"], document.header["id"], document.header["from"], document.header["subject"]) == (
        "result", "weekly-2026-10-05", "learn", "2026-10-05")
    assert "已修复 Issue 1 个(上周 无样本)" in document.conclusion and "修复一次通过率 无样本" in document.conclusion
    assert "统计周期 2026-10-05 至 2026-10-11(UTC+09:00)" in document.sections["done"]
    assert "| 已修复数 | all | 1 |" in document.sections["outputs"]
    assert [item["name"] for item in document.blocks["checks"]][0] == "探针漏跑"
    assert handoff_files.read(result.handoff)["subject"] == {"type": "week", "id": "2026-10-05"}
    snapshot = {(item.metric, item.dimension): item.value for item in metric_snapshots.for_week(world.conn, WEEK)}
    assert snapshot[("fixed-issues", "all")] == 1
    assert notifications.sent[0][:2] == ("learn-weekly", "2026-10-05")
    world.clock.advance(timedelta(hours=1))
    make_service(world).report()
    again = [item for item in metric_snapshots.for_week(world.conn, WEEK) if item.metric == "fixed-issues"]
    assert [(item.value, item.computed_at) for item in again] == [(1, NOW + timedelta(hours=1))]


def test_report_shows_last_week_from_snapshots(tmp_path):
    world = make_learn_world(tmp_path)
    last_week = [metrics.MetricValue.count("fixed-issues", "all", 3)]
    metrics.save_snapshots(world.conn, WEEK - timedelta(weeks=1), last_week, NOW)
    result = make_service(world).report()
    body = result.report.read_text(encoding="utf-8")
    assert "已修复 Issue 0 个(上周 3)" in body
    assert "| 已修复数 | all | 0 | 3 | 无样本 → 无样本 → 3 → 0 | 0 |" in body


def test_a_failing_metric_is_listed_and_the_rest_are_kept(tmp_path, monkeypatch):
    world = make_learn_world(tmp_path)

    def broken(ctx):
        raise ValueError("来源缺失")

    monkeypatch.setattr(metrics, "METRICS", (("noise", Stage.AGGREGATE, broken),
                                             *[item for item in metrics.METRICS if item[0] != "noise"]))
    result = make_service(world).report()
    assert {"item": "noise", "reason": "ValueError: 来源缺失"} in result.outputs["errors"]
    assert "- 统计出错：noise：ValueError: 来源缺失" in documents.read(result.report).sections["deviations"]
    assert any(item["metric"] == "fixed-issues" for item in result.outputs["metrics"])


def test_health_and_lessons_write_run_handoffs(tmp_path):
    world = make_learn_world(tmp_path)
    health = make_service(world).health()
    assert [item["check"] for item in health.outputs["health"]][0] == "missed-runs"
    assert handoff_files.read(health.handoff)["subject"] == {"type": "run", "id": health.run_id}
    problem_with(world, "P-0001")
    triaged(world, "P-0001", outcome=TriageOutcome.FALSE_CONFIRM, outcome_at=NOW)
    world.runner.add("lesson-writer", {"mode": "lesson", "draft": None, "contradictions": [], "duplicates": []})
    lessons = make_service(world).lessons()
    assert lessons.outputs["lessons"] == [{"source": "lesson:triage:P-0001:1", "knowledgeId": None, "decision": None}]
    assert handoff_files.read(lessons.handoff)["outputs"]["lessons"] == lessons.outputs["lessons"]
    assert [row.role for row in stage_yield.find(world.conn)] == ["lesson-writer"]


def test_output_mode_writes_nothing_to_the_database(tmp_path):
    world = make_learn_world(tmp_path)
    before = len(runs.find(world.conn))
    output = tmp_path / "out"
    result = make_service(world, output_dir=output).report()
    assert result.report == output / "weekly-2026-10-05.md" and result.report.is_file()
    assert result.handoff.is_relative_to(output)
    assert len(runs.find(world.conn)) == before
    assert metric_snapshots.for_week(world.conn, WEEK) == []
