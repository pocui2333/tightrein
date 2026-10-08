"""定案：确认绑定方案哈希；在自动确认的条件内自动通过，否则列出全部原因等用户；决定先持久化。"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from tightrein.implement.approve import approve
from tightrein.implement.context import DECISIONS, Decision
from tightrein.protocol.handoff import Status
from tightrein.protocol.naming import format_iso
from tightrein.store.tables import issues

DESIGN = "implement.design"


def _design(world: Any, **changes: Any) -> Any:
    facts: dict[str, Any] = {"summary": "查询改为最近 7 天", "planHash": "a" * 64, "userDecisions": [],
                             "flags": {}, "protectedTouches": [], "migration": None, "newDependencies": [],
                             "deletions": [], "risk": {"high": False, "reasons": []},
                             "estimate": {"files": 1, "lines": 4}, "frontendDesign": None,
                             "steps": [{"file": "src/orders.py", "change": "按 days 过滤"}]}
    facts.update(changes)
    return world.handoff(DESIGN, facts)


def _events(world: Any) -> list[str]:
    path = world.runtime.workspace.events(world.runtime.run)
    if not path.is_file():
        return []
    return [json.loads(line)["summary"] for line in path.read_text(encoding="utf-8").splitlines()]


def test_autonomy_confirms_a_simple_plan(world: Any) -> None:
    result = approve.run(world.runtime, world.context(latest={DESIGN: _design(world)}))
    assert result.status is Status.PASSED and result.facts["auto"] is True
    assert result.facts["planHash"] == "a" * 64 and result.facts["approvedBy"] == "auto"


def test_every_reason_against_autonomy_is_listed(world: Any) -> None:
    facts = _design(world).facts | {
        "userDecisions": [{"question": "保留旧接口吗"}],
        "flags": {"dataStructure": {"flagged": True, "reason": "加字段"}, "design": {"flagged": False}},
        "protectedTouches": [{"path": "package.json", "change": "加脚本"}],
        "migration": {"entries": ["001"]}, "newDependencies": [{"name": "dayjs", "version": "1.11"}],
        "deletions": [{"path": "src/old.py"}], "risk": {"high": True, "reasons": ["schema：含数据库迁移"]},
        "estimate": {"files": 4, "lines": 50}, "frontendDesign": {"planConflicts": ["分页也要改"]}}
    assert approve.auto_reasons(facts, world.runtime.settings) == [
        "需要拍板：保留旧接口吗", "标记 dataStructure：加字段", "改动高风险文件 package.json：加脚本", "含数据库迁移",
        "新增依赖 dayjs 1.11", "删除文件 src/old.py", "高风险：schema：含数据库迁移",
        "预估 4 个文件、50 行，超出自动确认门槛 3 个文件、100 行", "前端设计说明与方案冲突：分页也要改"]


def test_plans_needing_decisions_wait_and_the_reasons_are_recorded_once(world: Any) -> None:
    context = world.context(latest={DESIGN: _design(world, migration={"entries": ["001"]})})
    first = approve.run(world.runtime, context)
    assert first.status is Status.PENDING and first.facts["auto"] is False
    assert first.facts["reasons"] == ["含数据库迁移"] and "tightrein reject 0018" in first.facts["gate"]["options"][1]
    approve.run(world.runtime, context)
    assert _events(world) == ["方案需要用户确认：含数据库迁移"]


def test_a_manual_gate_always_waits(new_world: Any, settings_with: Any) -> None:
    world = new_world(settings=settings_with({"boundaries": {"gates": {"design": "manual"}}}))
    result = approve.run(world.runtime, world.context(latest={DESIGN: _design(world)}))
    assert result.status is Status.PENDING and result.facts["reasons"] == ["方案确认关卡配置为人工"]


def test_the_users_decision_is_bound_to_the_plan_hash(world: Any) -> None:
    design = _design(world, migration={"entries": ["001"]})
    waiting = world.handoff(approve.POINT, {"planHash": "a" * 64, "auto": False}, Status.PENDING)
    world.clock.advance(timedelta(minutes=5))
    at = format_iso(world.clock.now())
    approved = Decision(approve.POINT, "approve", 1, "可以", at)
    context = world.context(latest={DESIGN: design, approve.POINT: waiting}, decisions=[approved])
    result = approve.run(world.runtime, context)
    assert result.status is Status.PASSED and result.facts["approvedBy"] == "user" and result.facts["option"] == 1
    # 方案重出后哈希变了：旧的确认不算
    changed = _design(world, planHash="b" * 64, migration={"entries": ["001"]})
    context = world.context(latest={DESIGN: changed, approve.POINT: waiting}, decisions=[approved])
    assert approve.run(world.runtime, context).status is Status.PENDING
    rejected = Decision(approve.POINT, "reject", None, "不要改接口", at)
    context = world.context(latest={DESIGN: design, approve.POINT: waiting}, decisions=[rejected])
    result = approve.run(world.runtime, context)
    assert result.status is Status.FAILED and result.facts["rejected"] and result.facts["note"] == "不要改接口"


def test_coding_checks_the_confirmed_plan(world: Any) -> None:
    design = _design(world)
    passed = world.handoff(approve.POINT, {"planHash": "a" * 64, "auto": True})
    assert approve.confirmed(world.context(latest={DESIGN: design, approve.POINT: passed}))
    replanned = _design(world, planHash="b" * 64)
    assert not approve.confirmed(world.context(latest={DESIGN: replanned, approve.POINT: passed}))
    assert not approve.confirmed(world.context(latest={DESIGN: design}))


def test_decisions_are_persisted_and_a_rejection_needs_a_reason(world: Any) -> None:
    at = format_iso(world.clock.now())
    with pytest.raises(ValueError):
        approve.record(world.runtime, "0018", Decision(approve.POINT, "reject", None, " ", at))
    with pytest.raises(ValueError):
        approve.record(world.runtime, "0018", Decision(approve.POINT, "maybe", None, None, at))
    approve.record(world.runtime, "0018", Decision(approve.POINT, "approve", 2, "按选项 2", at))
    found = issues.get(world.runtime.conn, "0018")
    assert found is not None and found.extra[DECISIONS] == [
        {"point": approve.POINT, "verdict": "approve", "option": 2, "note": "按选项 2", "at": at}]
