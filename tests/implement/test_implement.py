"""实施的流程：从落盘的产物推导下一步，按性质分派没通过的步骤，停在关卡、停下或退回评估，写给人看的文档。"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from tightrein.assess.issue.transitions import IssueStatus, approve
from tightrein.implement import implement
from tightrein.implement.context import DECISIONS, load
from tightrein.implement.implement import Kind
from tightrein.protocol import recovery
from tightrein.protocol.handoff import Handoff, Status, read, write
from tightrein.protocol.naming import FileName, format_iso
from tightrein.store.tables import issues

ISSUE = "0018"
RUN = "R-20261008T030000Z-implement"
PREPARE, LOCATE, DESIGN, APPROVE, CODE, CHECK, REVIEW, DELIVER = implement.STEPS


class Budget:
    def __init__(self, used: float = 0.0, limit: float = 2_000_000.0) -> None:
        self.limit = limit
        self._used = used

    def used(self, subject: str) -> float:
        return self._used

    def exceeded(self, subject: str) -> bool:
        return self._used >= self.limit


class Steps:
    """假的小步骤：按点给出预置的交接(没给的步骤一律通过)，记下被调用的顺序；每一步耗时一秒(交接按完成时间排序)。"""

    def __init__(self, clock: Any) -> None:
        self.clock = clock
        self.planned: dict[str, list[tuple[Status, dict[str, Any]]]] = {}
        self.ran: list[str] = []

    def plan(self, point: str, status: Status, facts: dict[str, Any] | None = None) -> None:
        self.planned.setdefault(point, []).append((status, facts or {}))

    def runner(self, point: str) -> Any:
        def run(runtime: Any, context: Any) -> Handoff:
            self.ran.append(point)
            self.clock.advance(timedelta(seconds=1))
            queue = self.planned.get(point) or []
            status, facts = queue.pop(0) if queue else (Status.PASSED, {})
            return Handoff(point=point, subject=context.issue.id, run=runtime.run, status=status,
                           summary=f"{point} {status.value}", facts=dict(facts))
        return SimpleNamespace(run=run)


@pytest.fixture
def steps(world: Any, monkeypatch: pytest.MonkeyPatch) -> Steps:
    found = Steps(world.clock)
    for point in implement.STEPS:
        name = f"tightrein_test_steps.{point}"
        monkeypatch.setitem(sys.modules, name, found.runner(point))
        monkeypatch.setitem(implement.RUNNERS, point, name)
    world.runtime.agents = SimpleNamespace(budget=Budget(), breaker=SimpleNamespace(object_progressed=lambda s: None))
    return found


def _save(world: Any, point: str, status: Status = Status.PASSED, facts: dict[str, Any] | None = None, *,
          round: int | None = None, summary: str = "") -> Handoff:
    """按完成的先后落盘一份交接(每份晚一分钟)。"""
    world.clock.advance(timedelta(minutes=1))
    found = Handoff(point=point, subject=ISSUE, run=RUN, status=status, summary=summary or f"{point} {status.value}",
                    facts=facts or {}, round=round, created_at=format_iso(world.clock.now()))
    write(world.runtime.workspace.step_file(ISSUE, FileName(point, "handoff", "json", round=round)), found)
    return found


def _decide(world: Any) -> implement.Next:
    return implement.decide(world.runtime, load(world.runtime, ISSUE))


def _blocker(category: str, location: str = "src/orders.py:2", kind: str = "bug") -> dict[str, Any]:
    return {"check": "review", "kind": kind, "location": location, "summary": f"{kind} 没修好", "category": category}


def _planned(world: Any, *, version: int = 1) -> None:
    _save(world, PREPARE)
    _save(world, LOCATE)
    _save(world, DESIGN, facts={"version": version, "planHash": "a" * 64})
    _save(world, APPROVE, facts={"planHash": "a" * 64, "auto": True})


def _round(world: Any, number: int, blockers: list[dict[str, Any]], *, diff: str | None = None) -> None:
    _save(world, CODE, facts={"diffHash": diff or f"{number:064x}"}, round=number)
    _save(world, CHECK, round=number)
    _save(world, REVIEW, Status.FAILED, {"blockers": blockers}, round=number)


def _decision(world: Any, verdict: str, note: str | None = None, *, step: str = REVIEW) -> None:
    world.clock.advance(timedelta(minutes=1))
    found = issues.get(world.runtime.conn, ISSUE)
    decision = {"point": "needs_decision", "step": step, "verdict": verdict, "option": None, "note": note,
                "at": format_iso(world.clock.now())}
    issues.save(world.runtime.conn, replace(found, extra={**found.extra, DECISIONS: [decision]}), world.clock)


# 推导下一步


def test_the_steps_follow_in_order_and_rounds_are_numbered(world: Any) -> None:
    assert _decide(world) == implement.Next(Kind.RUN, PREPARE)
    _planned(world)
    assert _decide(world) == implement.Next(Kind.RUN, CODE, round=1)
    _save(world, CODE, round=1)
    assert _decide(world) == implement.Next(Kind.RUN, CHECK, round=1)
    _save(world, CHECK, round=1)
    _save(world, REVIEW, round=1)
    assert _decide(world) == implement.Next(Kind.RUN, DELIVER)
    _save(world, DELIVER)
    assert _decide(world).kind is Kind.DONE


def test_a_gate_waits_until_the_user_decides(world: Any) -> None:
    gate = {"decision": "确认方案", "command": "tightrein approve 0018"}
    _save(world, APPROVE, Status.PENDING, {"gate": gate})
    found = _decide(world)
    assert (found.kind, found.point, found.gate) == (Kind.WAIT, APPROVE, gate)
    _decision(world, "approve", step=APPROVE)
    assert _decide(world) == implement.Next(Kind.RUN, APPROVE)  # 由定案取用这个决定


def test_local_problems_go_back_to_coding_until_the_rounds_run_out(world: Any) -> None:
    _planned(world)
    _round(world, 1, [_blocker("local", kind="a")])
    assert _decide(world) == implement.Next(Kind.RUN, CODE, round=2)
    _round(world, 2, [_blocker("local", kind="b")])
    _round(world, 3, [_blocker("local", kind="c")])
    assert _decide(world) == implement.Next(Kind.RUN, CODE, round=4)
    _round(world, 4, [_blocker("local", kind="d")])
    found = _decide(world)
    assert (found.kind, found.code) == (Kind.STOP, "rounds_exceeded")  # 交回修改 3 轮仍未通过
    assert len(found.tried) == 4


def test_no_progress_stops_early(world: Any) -> None:
    _planned(world)
    _round(world, 1, [_blocker("local")])
    _round(world, 2, [_blocker("local")])  # 阻断项(位置, 类型)与上一轮相同
    assert _decide(world).code == "no_progress"
    world.clock.advance(timedelta(days=1))
    _decision(world, "approve", "换个思路")  # 用户给了新决定：从这里重新计轮
    assert _decide(world) == implement.Next(Kind.RUN, CODE, round=3)


def test_an_unchanged_diff_is_no_progress(world: Any) -> None:
    _planned(world)
    _round(world, 1, [_blocker("local", kind="a")], diff="f" * 64)
    _round(world, 2, [_blocker("local", kind="b")], diff="f" * 64)
    assert _decide(world).code == "no_progress"


def test_only_the_first_category_is_handled(world: Any) -> None:
    _planned(world)
    _round(world, 1, [_blocker("local"), _blocker("plan_gap", kind="gap")])
    assert _decide(world) == implement.Next(Kind.RUN, DESIGN)  # 方案缺口先于局部问题：重出方案
    _save(world, DESIGN, facts={"version": 2, "planHash": "b" * 64})
    _save(world, APPROVE, facts={"planHash": "b" * 64})
    _round(world, 2, [_blocker("plan_gap", kind="gap")])
    found = _decide(world)
    assert (found.kind, found.code) == (Kind.STOP, "replans_exceeded")  # controls.implement.design.rounds 为 1


def test_design_issues_stop_and_user_matters_wait(world: Any) -> None:
    _planned(world)
    _round(world, 1, [_blocker("needs_user", kind="cap"), _blocker("design", kind="design")])
    found = _decide(world)
    assert (found.kind, found.code) == (Kind.STOP, "design_issue") and found.reason.startswith("根因在设计本身")
    _round(world, 2, [_blocker("needs_user", kind="cap"), _blocker("local")])
    found = _decide(world)
    assert (found.kind, found.point, found.round) == (Kind.WAIT, REVIEW, 2)
    assert "cap 没修好" in found.gate["decision"] and found.gate["command"].startswith("tightrein approve 0018")


def test_a_boundary_violation_is_redone_once(world: Any) -> None:
    _planned(world)
    _save(world, CODE, Status.FAILED, {"violations": [{"kind": "forbidden", "path": ".env"}]}, round=1)
    assert _decide(world) == implement.Next(Kind.RUN, CODE, round=1)
    _save(world, CODE, Status.FAILED, {"violations": [{"kind": "forbidden", "path": ".env"}], "redo": True}, round=1)
    assert _decide(world).code == "boundary"


def test_an_oversized_plan_goes_back_to_assessment(world: Any) -> None:
    parts = [{"title": "拆一", "goal": "目标一", "acceptance": ["能用"]}, {"title": "拆二", "goal": "目标二",
                                                                         "acceptance": ["也能用"]}]
    _save(world, DESIGN, Status.FAILED, {"reason": "oversize", "splitBack": {"reason": "太大", "parts": parts}})
    found = _decide(world)
    assert found.kind is Kind.SPLIT and found.parts[0] == "拆一\n\n目标一\n\n验收标准：\n- 能用"
    _save(world, DESIGN, Status.FAILED, {"reason": "plan_rejected", "problems": ["预估超上限"]})
    found = _decide(world)
    assert (found.kind, found.code, found.tried) == (Kind.STOP, "plan_rejected", ("预估超上限",))


def test_a_rejected_plan_is_replanned(world: Any) -> None:
    _planned(world)
    _save(world, APPROVE, Status.FAILED, {"rejected": True, "note": "不要改接口"})
    assert _decide(world) == implement.Next(Kind.RUN, DESIGN)


def test_changes_after_review_go_back_to_review_only(world: Any) -> None:
    _planned(world)
    _save(world, CODE, round=1)
    _save(world, CHECK, round=1)
    _save(world, REVIEW, round=1)
    _save(world, DELIVER, Status.FAILED, {"backTo": REVIEW, "reruns": 0})
    assert _decide(world) == implement.Next(Kind.RUN, REVIEW, round=1)
    _save(world, DELIVER, Status.FAILED, {"backTo": REVIEW, "reruns": 1})
    assert _decide(world).kind is Kind.STOP


def test_a_review_without_a_result_is_rerun_once(world: Any) -> None:
    _planned(world)
    _save(world, CODE, round=1)
    _save(world, CHECK, round=1)
    _save(world, REVIEW, Status.FAILED, {"callFailed": True, "reruns": 0}, round=1)
    assert _decide(world) == implement.Next(Kind.RUN, REVIEW, round=1)
    _save(world, REVIEW, Status.FAILED, {"callFailed": True, "reruns": 1}, round=1)
    assert _decide(world).code == "review_failed"


def test_after_a_stop_the_users_decision_decides(world: Any) -> None:
    _planned(world)
    _round(world, 1, [_blocker("needs_user")])
    _save(world, PREPARE, Status.FAILED, {"reason": "config_error"})
    found = _decide(world)
    assert (found.code, found.advice) == ("config_error", "修正项目的检查命令、准备命令或环境后接着做")
    _decision(world, "approve", "已装好依赖", step=PREPARE)
    assert _decide(world) == implement.Next(Kind.RUN, PREPARE)
    _round(world, 2, [_blocker("local")])
    _decision(world, "reject", "换个做法")
    assert _decide(world) == implement.Next(Kind.RUN, DESIGN)


# 推进一步


def test_each_step_is_recorded_with_the_common_facts(world: Any, steps: Steps) -> None:
    outcome = implement.implement(world.runtime, ISSUE)
    assert (outcome.point, outcome.status, outcome.next_point) == (PREPARE, Status.PASSED, LOCATE)
    saved = read(world.runtime.workspace.step_file(ISSUE, FileName(PREPARE, "handoff", "json")))
    # status 与知识库按固定的键读：程序补上没给的
    assert saved.facts["skipped"] is None and saved.facts["knowledgeSuggestions"] == []
    assert saved.facts["reruns"] == 0 and saved.metrics.duration_ms is not None and saved.versions.settings
    record = issues.get(world.runtime.conn, ISSUE)
    assert record is not None and record.step == LOCATE and record.gate is None
    assert implement.implement(world.runtime, ISSUE).next_point == DESIGN and steps.ran == [PREPARE, LOCATE]


def test_a_stop_writes_the_failure_document_and_an_approval_continues(world: Any, steps: Steps) -> None:
    for _ in range(2):
        implement.implement(world.runtime, ISSUE)
    steps.plan(DESIGN, Status.FAILED, {"reason": "plan_rejected", "problems": ["预估超上限"]})
    outcome = implement.implement(world.runtime, ISSUE)
    assert outcome.status is Status.FAILED and outcome.point == DESIGN
    failure = world.runtime.workspace.human_document(ISSUE, "failure")
    assert "预估超上限" in failure.read_text(encoding="utf-8")
    record = issues.get(world.runtime.conn, ISSUE)
    assert record is not None and record.status == IssueStatus.NEEDS_DECISION and record.step == DESIGN
    with pytest.raises(implement.NotImplementable):
        implement.implement(world.runtime, ISSUE)
    # 命令行 approve：状态机放行(只记进历史)，下次推进时看得到这个决定，重做方案
    world.clock.advance(timedelta(minutes=1))
    approve(world.runtime, ISSUE, note="上限放宽了")
    outcome = implement.implement(world.runtime, ISSUE)
    assert (outcome.point, outcome.status, outcome.next_point) == (DESIGN, Status.PASSED, APPROVE)
    assert issues.get(world.runtime.conn, ISSUE).status == IssueStatus.IMPLEMENTING  # type: ignore[union-attr]


def test_a_gate_writes_the_pending_document(world: Any, steps: Steps) -> None:
    for _ in range(3):
        implement.implement(world.runtime, ISSUE)
    gate = {"decision": "确认方案：改查询", "options": ["通过", "不通过"], "recommendation": "通过", "reason": "含迁移",
            "ifNot": "停在定案", "command": "tightrein approve 0018"}
    steps.plan(APPROVE, Status.PENDING, {"gate": gate})
    outcome = implement.implement(world.runtime, ISSUE)
    assert outcome.status is Status.PENDING and outcome.next_point is None
    text = world.runtime.workspace.human_document(ISSUE, "pending").read_text(encoding="utf-8")
    assert "确认方案：改查询" in text and "含迁移" in text
    record = issues.get(world.runtime.conn, ISSUE)
    assert record is not None and record.gate == implement.GATE_DESIGN and record.step == APPROVE


def test_an_oversized_issue_is_split_back(world: Any, steps: Steps) -> None:
    for _ in range(2):
        implement.implement(world.runtime, ISSUE)
    parts = [{"title": f"拆分 {number}", "goal": "单独合入", "acceptance": ["能用"]} for number in (1, 2)]
    steps.plan(DESIGN, Status.FAILED, {"reason": "oversize", "splitBack": {"reason": "太大", "parts": parts}})
    outcome = implement.implement(world.runtime, ISSUE)
    record = issues.get(world.runtime.conn, ISSUE)
    assert record is not None and record.status == IssueStatus.CANCELLED
    created = [item for item in issues.find(world.runtime.conn) if item.extra.get("parent") == ISSUE]
    assert [item.title for item in created] == ["拆分 1", "拆分 2"]
    assert all(item.status == IssueStatus.TODO for item in created) and created[1].extra["dependsOn"] == created[0].id
    assert f"拆成 {created[0].id}、{created[1].id}" in outcome.summary and outcome.next_point is None


def test_delivery_hands_the_issue_to_release(world: Any, steps: Steps) -> None:
    for _ in range(7):
        implement.implement(world.runtime, ISSUE)
    outcome = implement.implement(world.runtime, ISSUE)
    assert (outcome.point, outcome.status, outcome.next_point) == (DELIVER, Status.PASSED, None)
    assert steps.ran == list(implement.STEPS)
    assert issues.get(world.runtime.conn, ISSUE).status == IssueStatus.RELEASING  # type: ignore[union-attr]


def test_the_budget_and_manual_takeover_stop_it(world: Any, steps: Steps) -> None:
    world.runtime.agents.budget = Budget(used=2_000_000)
    outcome = implement.implement(world.runtime, ISSUE)
    assert outcome.status is Status.FAILED and "用量已达上限" in outcome.summary and steps.ran == []
    found = issues.get(world.runtime.conn, ISSUE)
    issues.save(world.runtime.conn, replace(found, status=IssueStatus.HELD, held_by="cty"), world.clock)
    with pytest.raises(implement.NotImplementable, match="手动接管"):
        implement.implement(world.runtime, ISSUE)


def test_a_half_done_step_is_thrown_away(world: Any, steps: Steps) -> None:
    """中断后没做完的那一步整个丢掉：worktree 与上一个检查点不一致时退回去，再从产物推导下一步。"""
    _save(world, PREPARE, facts={"worktree": str(world.repo.path), "baseCommit": world.repo.base,
                                 "worktreeCommit": world.repo.base})
    world.repo.write({"src/orders.py": "half done\n", "src/scratch.py": "X = 1\n"})
    outcome = implement.implement(world.runtime, ISSUE)
    assert (outcome.point, outcome.next_point) == (LOCATE, DESIGN)
    assert (world.repo.path / "src/orders.py").read_text(encoding="utf-8") == "def recent(days):\n    return days\n"
    assert not (world.repo.path / "src/scratch.py").exists()
    saved = read(world.runtime.workspace.step_file(ISSUE, FileName(LOCATE, "handoff", "json")))
    assert saved.facts["worktreeCommit"] == world.repo.base  # 每一步完成时记下 worktree 的快照


def test_a_regressed_issue_starts_over_without_the_last_attempts_steps(world: Any, steps: Steps) -> None:
    """回归后退回待修(issues.extra.attempt 为 2)：上一次各步的交接挪进 attempt_1/，这一次从准备做起，不判为已交付。"""
    for point in implement.STEPS:
        _save(world, point, round=1 if point in implement.ROUND_POINTS else None)
    found = issues.get(world.runtime.conn, ISSUE)
    issues.save(world.runtime.conn, replace(found, status=IssueStatus.TODO, stage=None, step=None,
                                            extra={**found.extra, "attempt": 2}), world.clock)
    outcome = implement.implement(world.runtime, ISSUE)
    assert (outcome.point, outcome.next_point) == (PREPARE, LOCATE) and steps.ran == [PREPARE]
    archived = world.runtime.workspace.attempt_dir(ISSUE, 1)
    assert (archived / "38-implement.deliver-handoff.json").is_file()
    assert [item.handoff.point for item in recovery.checkpoints(world.runtime.workspace, ISSUE)] == [PREPARE]
