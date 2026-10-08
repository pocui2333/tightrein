"""评估的整条流程：选题 → 分情况 → 查重 → 取证 → 复核 → 评级 → 去向 → 落库 → 写成 Issue。模型调用是录制的输出。"""

import json
from types import SimpleNamespace

import pytest

from tightrein.agents.result import CallStatus
from tightrein.assess import assess as assessing
from tightrein.assess import claims, persist
from tightrein.assess.issue import create
from tightrein.collect.dedup import suppress
from tightrein.protocol.git import GitError
from tightrein.protocol.handoff import Status
from tightrein.store.files.json import read_json
from tightrein.store.tables import issues, problems

TRIAGE, REFUTE = "assess.triage", "assess.refute"


def handoff(layout, subject: str) -> dict:
    return read_json(layout.subject_dir(subject) / "21-assess.triage-handoff.json")


def test_a_confirmed_low_risk_problem_becomes_an_approved_issue(runtime, make_problem, agent, readonly, outputs,
                                                                layout, kit):
    make_problem()
    agent.queue(TRIAGE, outputs.confirmed(knowledgeSuggestions=["订单查询一律带用户条件"]))
    outcome = assessing.assess(runtime, "P-0001")
    assert (outcome.verdict, outcome.severity, outcome.disposition, outcome.issue, outcome.status) == (
        "confirmed", "P2", "fix_later", "0001", Status.PASSED)
    assert agent.points() == [TRIAGE]  # 没有候选不查重；P2 不复核
    problem = problems.get(runtime.conn, "P-0001")
    assert problem.status == "ongoing" and problem.issue == "0001"
    assert problem.extra["verdict"] == "confirmed" and problem.extra["severity"] == "P2"
    assert problem.extra["assess"]["commit"] == kit.COMMIT and problem.extra["assess"]["case"] == "light"
    issue = issues.get(runtime.conn, "0001")
    assert issue.status == "todo" and issue.extra["approvedAt"]  # 低风险自动放行
    assert issue.extra["introducedBy"] == [{"commit": "a" * 40, "author": "zhang", "pr": None}]
    facts = handoff(layout, "P-0001")["facts"]
    assert facts["knowledgeSuggestions"] == ["订单查询一律带用户条件"] and facts["misjudged"] is None
    assert facts["issues"] == ["0001"] and facts["case"] == "light"
    assert (layout.subject_dir("0001") / "22-assess.issue-handoff.json").is_file()
    assert (layout.subject_dir("0001") / "00-issue-notes.json").is_file()  # 代码笔记交给实施
    notes = read_json(layout.subject_dir("P-0001") / "00-problem-notes.json")
    assert {entry["location"] for entry in notes["entries"]} == {"routes/orders.py:4", "services/orders.py:3"}
    assert (layout.subject_dir("P-0001") / "00-problem-assess.json").is_file()  # 重建用的快照


def test_a_risky_issue_waits_for_approval(runtime, make_problem, agent, readonly, outputs, layout):
    make_problem()
    output = outputs.confirmed()
    output["assessment"]["taskType"] = "security"
    agent.queue(TRIAGE, output)
    outcome = assessing.assess(runtime, "P-0001")
    issue = issues.get(runtime.conn, outcome.issue)
    assert issue.status == "needs_decision" and issue.gate == "issue" and issue.extra["approvedAt"] is None
    assert layout.human_document(issue.id, "pending").is_file()


def test_high_risk_conclusions_are_refuted_and_disagreement_goes_to_the_manual_queue(
        runtime, make_problem, agent, readonly, outputs, layout):
    make_problem(flags={"deterministic": True})
    confirmed = outputs.confirmed()
    confirmed["report"]["severity"] = "P1"
    agent.queue(TRIAGE, confirmed)
    agent.queue(REFUTE, outputs.refuted())
    outcome = assessing.assess(runtime, "P-0001")
    assert agent.points() == [TRIAGE, REFUTE]
    assert outcome.destination == "manual" and outcome.verdict == "confirmed"
    problem = problems.get(runtime.conn, "P-0001")
    assert problem.status == "new" and problem.extra["assess"]["manual"]  # 保持原状态，不再自动评估
    assert layout.human_document("P-0001", "pending").is_file()
    assert "confirmed" not in agent.calls[1].prompt.split("# 本次输入\n")[1]  # 盲审：看不到第一次的判定


@pytest.mark.parametrize("flags", [{"severityHint": "P0"}, {"severityHint": "P0", "reproducible": True}])
def test_a_refuted_p0_is_refuted_again(runtime, make_problem, agent, readonly, outputs, flags):
    make_problem(flags=flags)
    agent.queue(TRIAGE, outputs.refuted())
    agent.queue(REFUTE, outputs.refuted())
    outcome = assessing.assess(runtime, "P-0001")
    assert agent.points() == [TRIAGE, REFUTE]  # 能复现的也要复核：P0 的不成立须经复核
    assert outcome.destination == "false_positive"
    assert problems.get(runtime.conn, "P-0001").status == "closed"
    assert [rule.fingerprint for rule in suppress.load(runtime.conn, [])] == ["fp-P-0001"]


def test_a_refuted_p0_confirmed_by_the_refuter_goes_to_the_manual_queue(runtime, make_problem, agent, readonly,
                                                                         outputs):
    make_problem(flags={"severityHint": "P0"})
    agent.queue(TRIAGE, outputs.refuted())
    agent.queue(REFUTE, outputs.confirmed())
    outcome = assessing.assess(runtime, "P-0001")
    assert outcome.destination == "manual" and outcome.verdict == "confirmed"


def test_evidence_that_keeps_failing_the_checks_goes_to_the_manual_queue(runtime, make_problem, agent, readonly,
                                                                          outputs):
    make_problem()
    agent.queue(TRIAGE, outputs.confirmed(trigger="可能是并发"), CallStatus.TIMEOUT)
    outcome = assessing.assess(runtime, "P-0001")
    assert outcome.verdict == "insufficient" and outcome.destination == "manual"
    assert "重做后仍未通过" in outcome.reason


def test_insufficient_evidence_is_watched_first(runtime, make_problem, agent, readonly, outputs):
    make_problem()
    agent.queue(TRIAGE, outputs.insufficient())
    outcome = assessing.assess(runtime, "P-0001")
    assert outcome.destination == "watch_evidence"
    problem = problems.get(runtime.conn, "P-0001")
    assert problem.status == "watching" and problem.extra["assess"]["insufficient"] == 1


def test_fixed_on_main_waits_for_the_deployment(runtime, make_problem, agent, readonly, outputs):
    make_problem()
    agent.queue(TRIAGE, outputs.confirmed(fixedOnMain={"commit": "d" * 12, "basis": "该提交加了用户条件"}))
    outcome = assessing.assess(runtime, "P-0001")
    assert outcome.destination == "awaiting_deploy" and outcome.issue is None
    assert problems.get(runtime.conn, "P-0001").status == "ongoing"
    assert issues.find(runtime.conn) == []


def test_a_merged_problem_is_closed_without_a_verdict(runtime, make_problem, make_issue, agent, readonly):
    make_problem("P-0002", status="ongoing", issue="0007", title="保存订单时报错")
    make_issue("0007", status="todo", problems_=["P-0002"], title="保存订单时报错")
    make_problem("P-0001")
    outcome = assessing.assess(runtime, "P-0001")
    assert outcome.merged_into == "P-0002" and outcome.destination == "merged" and agent.calls == []
    assert problems.get(runtime.conn, "P-0001").extra["mergedInto"] == "P-0002"


def test_a_program_error_fails_only_that_problem(runtime, make_problem, agent, readonly, outputs, layout, kit,
                                                 monkeypatch):
    runtime.settings = kit.make_settings({"resources": {"concurrency": {"modelCalls": 1}}})
    make_problem("P-0001")
    make_problem("P-0002", location="routes/orders.py:4")
    build = claims.build

    def broken(problem, *args, **kwargs):
        if problem.id == "P-0001":
            raise KeyError("missing")
        return build(problem, *args, **kwargs)

    monkeypatch.setattr(claims, "build", broken)
    agent.queue(TRIAGE, outputs.confirmed())
    found = {outcome.problem: outcome for outcome in assessing.assess_pending(runtime)}
    assert found["P-0001"].status == Status.FAILED and "assess.py:" in found["P-0001"].reason  # 带出错位置
    assert found["P-0002"].status == Status.PASSED
    failed = handoff(layout, "P-0001")
    assert failed["status"] == "failed" and failed["facts"]["knowledgeSuggestions"] == []


def test_budget_locks_and_worktree_failures_stop_or_skip(runtime, make_problem, agent, readonly, layout, kit,
                                                         monkeypatch):
    make_problem()
    lock = layout.object_lock("P-0001")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": 1, "host": "other-host", "acquiredAt": "2026-10-08T03:00:00Z",
                                "heartbeatAt": "2026-10-08T03:00:00Z"}), encoding="utf-8")
    outcome = assessing.assess(runtime, "P-0001")
    assert outcome.status == Status.PENDING and outcome.reason == assessing.LOCKED
    lock.unlink()
    runtime.agents.quota = SimpleNamespace(reserve_reached=lambda: True)
    assert [item.reason for item in assessing.assess_pending(runtime)] == [assessing.RESERVE]
    assert agent.calls == []


def test_a_worktree_that_cannot_move_to_main_stops_the_whole_run(runtime, make_problem, monkeypatch):
    make_problem()
    runtime.git = None
    monkeypatch.setattr(assessing, "create_readonly", lambda *args, **kwargs: None)

    def failing(*args, **kwargs):
        raise GitError("fetch failed")

    monkeypatch.setattr(assessing, "sync_readonly", failing)
    with pytest.raises(assessing.AssessBlocked):
        assessing.assess_pending(runtime)


def test_retriage_with_a_note_and_a_user_override(runtime, make_problem, agent, readonly, outputs, layout):
    make_problem()
    agent.queue(TRIAGE, outputs.insufficient(), outputs.refuted())
    assessing.assess(runtime, "P-0001")
    outcome = assessing.retriage(runtime, "P-0001", "只在月底出现")
    assert "只在月底出现(用户提供)" in agent.calls[1].prompt
    assert "full" in agent.calls[1].prompt.split("## 情况")[1].split("##")[0]  # 重新评估一律完整取证
    assert outcome.destination == "false_positive"
    record = problems.get(runtime.conn, "P-0001").extra["assess"]
    assert record["attempt"] == 2 and record["userNotes"] == ["只在月底出现"]
    assert record["previous"][0]["verdict"] == "insufficient"
    overridden = assessing.override(runtime, "P-0001", "confirmed", "用户在测试环境复现了")
    assert overridden.issue is not None and agent.calls[2:] == []  # 改判不调用模型
    facts = handoff(layout, "P-0001")["facts"]
    assert facts["misjudged"]["kind"] == persist.FALSE_REFUTE and facts["override"] is True
    after = problems.get(runtime.conn, "P-0001")
    assert after.status == "ongoing" and after.extra["assess"]["commit"] == record["commit"]  # 沿用上一次的取证 commit
    assert after.extra["assess"]["previous"][-1]["outcome"] == persist.FALSE_REFUTE


def test_new_issue_is_an_assess_entry_point():
    assert assessing.new_issue is create.new_issue
