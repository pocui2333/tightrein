"""合并队列：按严重度、再按放行时间排(手动接管的不排)；依次处理，转为待决定的移出队列继续后面的；遇到人工合并
关卡即停；到时间上限即停。"""

from datetime import timedelta

from tightrein.protocol.handoff import Status
from tightrein.release import queue as merge_queue
from tightrein.store.tables import issues


def _issues(kit, runtime):
    kit.new_issue(runtime, issue_id="0001", severity="P2", extra={"approvedAt": "2026-10-07T01:00:00Z"})
    kit.new_issue(runtime, issue_id="0002", severity="P0", extra={"approvedAt": "2026-10-07T05:00:00Z"})
    kit.new_issue(runtime, issue_id="0003", severity="P2", extra={"approvedAt": "2026-10-06T01:00:00Z"})
    kit.new_issue(runtime, issue_id="0004", severity=None)
    held = kit.new_issue(runtime, issue_id="0005", severity="P0")
    held.held_by = "cty"
    issues.save(runtime.conn, held, runtime.clock)


def test_the_queue_orders_by_severity_then_approval(kit, runtime):
    _issues(kit, runtime)
    found = merge_queue.order(issues.find(runtime.conn, status="releasing"))
    assert [issue.id for issue in found] == ["0002", "0003", "0001", "0004"]


def test_failed_items_leave_the_queue_and_a_person_gate_stops_it(kit, runtime, monkeypatch):
    _issues(kit, runtime)
    seen = []

    def release(found_runtime, issue_id):
        from tightrein.implement.implement import StepOutcome

        seen.append(issue_id)
        issue = issues.get(found_runtime.conn, issue_id)
        if issue_id == "0002":  # 同步冲突：转为待决定，移出队列
            issue.status = "needs_decision"
            issues.save(found_runtime.conn, issue, found_runtime.clock)
            return StepOutcome(issue_id, "release.pr", Status.PENDING, "冲突", "release.pr")
        if issue_id == "0003":  # 合并了，接着下一个
            return StepOutcome(issue_id, "release.merge", Status.PASSED, "已合并", "release.deploy")
        return StepOutcome(issue_id, "release.merge", Status.PENDING, "高风险路径", "release.merge")

    monkeypatch.setattr(merge_queue, "release", release)
    outcomes = merge_queue.queue(runtime)
    assert seen == ["0002", "0003", "0001"]
    assert [outcome.subject for outcome in outcomes] == seen


def test_the_time_limit_stops_the_loop(kit, runtime, monkeypatch):
    _issues(kit, runtime)
    seen = []

    def release(found_runtime, issue_id):
        from tightrein.implement.implement import StepOutcome

        seen.append(issue_id)
        found_runtime.clock.advance(timedelta(minutes=40))
        return StepOutcome(issue_id, "release.merge", Status.PASSED, "已合并", "release.deploy")

    monkeypatch.setattr(merge_queue, "release", release)
    merge_queue.queue(runtime)
    assert seen == ["0002", "0003"]  # 缺省上限 1h
