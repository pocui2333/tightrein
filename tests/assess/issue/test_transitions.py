from datetime import timedelta

import pytest

from tightrein.assess import persist
from tightrein.assess.issue import files, transitions
from tightrein.assess.issue.transitions import InvalidTransition, IssueEvent, check, transition
from tightrein.collect.dedup import suppress
from tightrein.store.files.json import read_json
from tightrein.store.tables import issues, problems
from tightrein.store.tables.issues import Issue

E = IssueEvent


def record(status: str, *, origin: str = "problem", stage: str | None = None, step: str | None = None,
           **extra) -> Issue:
    return Issue(id="0007", status=status, title="t", kind="bug", origin=origin, stage=stage, step=step,
                 held_by="user" if status == "held" else None, extra=dict(extra))


def move(issue: Issue, event: IssueEvent, clock, reason: str | None = None) -> Issue:
    return transition(issue, event, reason=reason, clock=clock)


@pytest.mark.parametrize("event, source, reason, origin, target", [
    (E.APPROVE, "needs_decision", None, "problem", "todo"),
    (E.APPROVE, "todo", None, "user", "todo"),  # 用户需求已是待修：不改状态
    (E.REJECT, "needs_decision", "duplicate", "problem", "cancelled"),
    (E.REJECT, "needs_decision", "不值得修", "problem", "cancelled"),
    (E.START, "todo", None, "problem", "implementing"),
    (E.START, "implementing", None, "problem", "implementing"),
    (E.START, "releasing", None, "problem", "implementing"),  # 合并主干后改到同一文件：退回实施重新审查
    (E.DELIVER, "implementing", None, "problem", "releasing"),
    (E.MERGE, "releasing", None, "problem", "accepting"),
    (E.ACCEPT, "accepting", None, "problem", "done"),
    (E.REGRESS, "accepting", None, "problem", "todo"),
    (E.REGRESS, "done", None, "problem", "todo"),
    (E.FAIL, "releasing", "fix_rejected", "problem", "cancelled"),
    (E.FAIL, "implementing", "not_reproduced", "problem", "needs_decision"),
    (E.FAIL, "releasing", "冲突", "problem", "needs_decision"),
    (E.CANCEL, "todo", "wont_fix", "problem", "cancelled"),
    (E.CANCEL, "accepting", "not_a_bug", "problem", "cancelled"),
    (E.REOPEN, "cancelled", None, "problem", "todo"),
    (E.TAKE, "implementing", None, "problem", "held"),
    (E.SPLIT_BACK, "implementing", None, "problem", "cancelled"),
])
def test_transition_table(clock, event, source, reason, origin, target):
    before = record(source, origin=origin, closeReason={"done": "fixed", "cancelled": "wont_fix"}.get(source))
    if before.extra["closeReason"] is None:
        before.extra.pop("closeReason")
    assert move(before, event, clock, reason).status == target


@pytest.mark.parametrize("event, source, reason", [
    (E.APPROVE, "todo", None),
    (E.START, "done", None),
    (E.DELIVER, "todo", None),
    (E.MERGE, "implementing", None),
    (E.ACCEPT, "releasing", None),
    (E.REOPEN, "todo", None),
    (E.GIVE, "todo", None),
    (E.SPLIT_BACK, "releasing", None),
])
def test_illegal_transitions_name_the_allowed_events(clock, event, source, reason):
    with pytest.raises(InvalidTransition) as error:
        move(record(source), event, clock, reason)
    assert f"当前为 {source}" in str(error.value) and "此时可用" in str(error.value)


@pytest.mark.parametrize("reason", [None, "fixed", "fix_rejected", "split"])
def test_user_cannot_close_as_fixed_or_rejected(clock, reason):
    with pytest.raises(InvalidTransition):
        move(record("todo"), E.CANCEL, clock, reason)


def test_reject_with_a_note_closes_as_wont_fix(clock):
    assert move(record("needs_decision"), E.REJECT, clock, "不值得修").extra["closeReason"] == "wont_fix"
    assert move(record("needs_decision"), E.REJECT, clock, "not_a_bug").extra["closeReason"] == "not_a_bug"


def test_a_hold_records_where_it_stopped_and_is_cleared_on_leaving(clock):
    stopped = move(record("implementing", stage="implement", step="implement.code"), E.FAIL, clock, "rounds_exceeded")
    assert stopped.status == "needs_decision" and stopped.step == "implement.code"  # 停下保留所在的步骤
    assert stopped.extra["hold"] == {"reason": "rounds_exceeded", "since": "2026-10-08T03:00:00Z",
                                     "stage": "implement", "status": "implementing"}
    approved = move(stopped, E.APPROVE, clock)  # 实施中停下的：放行后回到实施，从停下的步骤接着做
    assert approved.status == "implementing" and "hold" not in approved.extra
    assert (approved.stage, approved.step) == ("implement", "implement.code")
    gate = move(record("needs_decision", approvedAt=None), E.APPROVE, clock)  # 立项放行
    assert gate.status == "todo" and gate.stage is None


@pytest.mark.parametrize("status, expected", [("releasing", "releasing"), ("accepting", "accepting")])
def test_an_approval_after_a_release_stop_goes_back_to_where_it_stopped(clock, status, expected):
    """发布阶段经 FAIL 转为待决定的，放行后回到停下前的状态，从停下的步骤接着做。"""
    stopped = move(record(status, stage="release", step="release.pr", approvedAt="2026-10-01T00:00:00Z"),
                   E.FAIL, clock, "合并主干有冲突")
    clock.advance(timedelta(hours=2))
    approved = move(stopped, E.APPROVE, clock)
    assert approved.status == expected and approved.step == "release.pr" and approved.stage == "release"
    assert approved.extra["approvedAt"] == "2026-10-01T00:00:00Z"  # 合并队列按第一次放行排序，不重排到队尾


def test_the_first_approval_writes_approved_at(clock):
    approved = move(record("needs_decision", approvedAt=None), E.APPROVE, clock)
    assert approved.extra["approvedAt"] == "2026-10-08T03:00:00Z"


def test_starting_again_from_release_resets_the_step(clock):
    moved = move(record("releasing", stage="release", step="release.pr"), E.START, clock)
    assert (moved.status, moved.stage, moved.step) == ("implementing", "implement", None)
    merged = move(record("releasing", stage="release", step="release.merge"), E.MERGE, clock)
    assert merged.step == transitions.ACCEPT_STEP


def test_take_and_give_restore_the_previous_state(clock):
    taken = move(record("releasing", stage="release", step="release.ci"), E.TAKE, clock, "zhang")
    assert taken.held_by == "zhang" and taken.extra["heldFrom"] == "releasing" and taken.step == "release.ci"
    given = move(taken, E.GIVE, clock)
    assert given.status == "releasing" and given.held_by is None and "heldFrom" not in given.extra


def test_giving_back_without_the_state_before_the_takeover_is_an_error(clock):
    with pytest.raises(ValueError, match="没有记下接管前的状态"):
        move(record("held"), E.GIVE, clock)


def test_in_progress_issues_must_carry_their_stage(clock):
    check(record("implementing", stage="implement"))
    check(record("accepting", stage="release"))
    for bad in (record("implementing"), record("releasing", stage="implement"), record("accepting")):
        with pytest.raises(ValueError, match="必须带阶段"):
            check(bad)
    # 没记下阶段的(旧记录、交还)在转换时按状态补上
    assert move(record("implementing"), E.START, clock).stage == "implement"
    taken = move(record("releasing", stage="release"), E.TAKE, clock, "zhang")
    assert move(taken, E.GIVE, clock).stage == "release"


def test_issue_invariants():
    check(record("todo"))
    check(record("done", closeReason="fixed"))
    check(record("needs_decision", hold={"reason": "x"}))
    for bad in (record("done"), record("todo", closeReason="fixed"), record("cancelled", closeReason="fixed"),
                record("done", closeReason="wont_fix"), record("todo", hold={"reason": "x"}),
                record("cancelled", closeReason="unknown"), Issue("0007", "held", "t", "bug", "problem")):
        with pytest.raises(ValueError):
            check(bad)


def test_effects_follow_the_rule_and_the_regression_event():
    assert transitions.effects_for(record("todo"), E.CANCEL, "not_a_bug") == (
        transitions.Effect.SYNC_PROBLEMS, transitions.Effect.GITHUB, transitions.Effect.FILL_FALSE_CONFIRM)
    assert transitions.effects_for(record("implementing"), E.FAIL, "not_reproduced") == (
        transitions.Effect.FILL_FALSE_CONFIRM, transitions.Effect.REQUEST_RETRIAGE)
    assert transitions.regression_event(record("accepting")) is E.REGRESS
    assert transitions.regression_event(record("cancelled", closeReason="wont_fix")) is E.REGRESS
    assert transitions.regression_event(record("implementing")) is None


# apply_event：副作用、历史、文件与数据库一起


@pytest.fixture
def linked(make_problem, make_issue):
    """一个评估过、关联到 Issue 0007 的问题。"""
    make_problem("P-0001", status="ongoing", issue="0007",
                 extra={"assess": {"attempt": 1, "verdict": "confirmed"}, "aliases": ["fp-old"]})
    return make_issue("0007", status="todo", problems_=["P-0001"])


def test_approve_moves_to_todo_and_illegal_commands_name_the_allowed_ones(runtime, make_issue, layout):
    make_issue("0008", status="needs_decision")
    layout.human_document("0008", "pending").parent.mkdir(parents=True, exist_ok=True)
    layout.human_document("0008", "pending").write_text("待审核", encoding="utf-8")
    approved = transitions.approve(runtime, "0008", note="看过了")
    assert approved.status == "todo" and not layout.human_document("0008", "pending").exists()
    stored = read_json(files.record_path(layout, "0008"))
    assert stored["status"] == "todo" and stored["extra"]["history"][-1]["event"] == "approve"
    assert "approve(操作者 user)：看过了" in files.read_body(layout, "0008")
    with pytest.raises(InvalidTransition, match="此时可用"):
        transitions.approve(runtime, "0008")


def test_not_a_bug_suppresses_the_problems_and_records_a_false_confirm(runtime, linked, layout):
    transitions.close(runtime, "0007", "not_a_bug", note="是有意为之")
    problem = problems.get(runtime.conn, "P-0001")
    assert problem.status == "closed" and problem.extra["assess"]["outcome"] == persist.FALSE_CONFIRM
    assert {rule.fingerprint for rule in suppress.load(runtime.conn, [])} == {"fp-P-0001", "fp-old"}
    misjudged = read_json(layout.problem_dir("P-0001") / "21-assess.triage.r1-handoff.json")["facts"]["misjudged"]
    assert misjudged["kind"] == "false_confirm" and "不是缺陷" in misjudged["detail"]
    assert read_json(persist.snapshot_path(layout, "P-0001"))["row"]["status"] == "closed"  # 重建用


def test_wont_fix_ignores_the_problems_until_they_change(runtime, linked):
    transitions.close(runtime, "0007", "wont_fix")
    problem = problems.get(runtime.conn, "P-0001")
    assert problem.status == "muted"
    assert problem.extra["ignore"]["newRelease"] and problem.extra["ignore"]["severityEscalated"]


def test_duplicate_moves_the_problems_to_the_other_issue(runtime, linked, make_issue):
    make_issue("0009", status="todo", problems_=["P-0005"])
    with pytest.raises(ValueError):
        transitions.close(runtime, "0007", "duplicate")
    transitions.close(runtime, "0007", "duplicate", duplicate_of="0009")
    target = issues.get(runtime.conn, "0009")
    assert target.extra["problems"] == ["P-0005", "P-0001"]
    assert "以重复关闭" in target.extra["history"][-1]["note"]
    assert problems.get(runtime.conn, "P-0001").issue == "0009"
    assert issues.get(runtime.conn, "0007").extra["duplicateOf"] == "0009"


def test_not_reproduced_requests_a_retriage_and_updates_are_written_with_the_status(runtime, linked):
    transitions.apply_event(runtime, "0007", E.START, actor="tightrein")
    moved = transitions.apply_event(runtime, "0007", E.FAIL, reason="not_reproduced", actor="tightrein",
                                    note="按复现步骤没出现", updates={"step": "implement.check", "round": 2})
    assert moved.status == "needs_decision" and (moved.step, moved.round) == ("implement.check", 2)
    record_ = problems.get(runtime.conn, "P-0001").extra["assess"]
    assert record_["retriage"] and record_["outcome"] == persist.FALSE_CONFIRM
    assert "按复现步骤没出现" in record_["retriageNotes"][0]
    with pytest.raises(ValueError):
        transitions.apply_event(runtime, "0007", E.APPROVE, updates={"status": "done"})


def test_accept_fills_the_correct_outcome(runtime, linked):
    for event in (E.START, E.DELIVER, E.MERGE, E.ACCEPT):
        transitions.apply_event(runtime, "0007", event, actor="tightrein")
    assert issues.get(runtime.conn, "0007").extra["closeReason"] == "fixed"
    assert problems.get(runtime.conn, "P-0001").extra["assess"]["outcome"] == persist.CORRECT


def test_a_failed_transition_restores_the_files(runtime, linked, layout, monkeypatch):
    body_before = files.read_body(layout, "0007")
    record_before = files.record_path(layout, "0007").read_text(encoding="utf-8")

    def broken(*args, **kwargs):
        persist.update(runtime, problems.get(runtime.conn, "P-0001"), status="closed")
        raise RuntimeError("写到一半")

    monkeypatch.setattr(persist, "close_problems", broken)
    with pytest.raises(RuntimeError):
        transitions.close(runtime, "0007", "wont_fix")
    assert files.read_body(layout, "0007") == body_before
    assert files.record_path(layout, "0007").read_text(encoding="utf-8") == record_before
    assert issues.get(runtime.conn, "0007").status == "todo"
    assert problems.get(runtime.conn, "P-0001").status == "ongoing"
    assert not persist.snapshot_path(layout, "P-0001").exists()


def test_regressions_reopen_closed_issues_and_fail_accepting_ones(runtime, make_issue):
    make_issue("0007", status="cancelled", extra={"closeReason": "wont_fix"})
    make_issue("0008", status="accepting", stage="release", step="release.accept")
    make_issue("0009", status="implementing", stage="implement")
    assert transitions.on_regression(runtime, "0007", "P-0001").status == "todo"
    assert transitions.on_regression(runtime, "0008", "P-0001").status == "todo"
    assert transitions.on_regression(runtime, "0009", "P-0001") is None  # 还在修的不用处理
    assert "closeReason" not in issues.get(runtime.conn, "0007").extra


def test_edit_validates_records_the_changed_sections_and_keeps_the_file_on_failure(runtime, make_issue, layout):
    make_issue("0007", status="todo")
    original = files.read_body(layout, "0007")
    broken = original.replace("## 原因", "## 其他")
    problems_ = transitions.save_edit(runtime, "0007", broken)
    assert problems_ == ["缺少小节「原因」或它是空的"] and files.read_body(layout, "0007") == original
    edited = original.replace("返回 404", "返回 404，并记一条审计日志")
    assert transitions.save_edit(runtime, "0007", edited) == []
    history = issues.get(runtime.conn, "0007").extra["history"][-1]
    assert history["event"] == "edit" and history["note"] == "修改了 acceptance"
    assert "并记一条审计日志" in files.read_body(layout, "0007")
