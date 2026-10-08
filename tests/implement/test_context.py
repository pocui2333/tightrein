"""实施的上下文：从 store 与各步交接读出；用户的决定两个来源合并；知识条目按文件命中；基准只算一次。"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from tightrein.assess import notes as notes_file
from tightrein.assess.notes import CodeNotes
from tightrein.implement import context as context_module
from tightrein.implement.context import DECISIONS, Decision, Risk, load
from tightrein.protocol.handoff import Handoff, Status, write
from tightrein.protocol.naming import FileName, format_iso
from tightrein.store.tables import issues

RUN = "R-20261008T030000Z-implement"


def _save(world: Any, point: str, facts: dict[str, Any], *, minutes: int = 0) -> Handoff:
    at = world.clock.now() + timedelta(minutes=minutes)
    found = Handoff(point=point, subject="0018", run=RUN, status=Status.PASSED, summary=point, facts=facts,
                    created_at=format_iso(at))
    write(world.runtime.workspace.step_file("0018", FileName(point, "handoff", "json")), found)
    return found


def _update(world: Any, **extra: Any) -> None:
    found = issues.get(world.runtime.conn, "0018")
    issues.save(world.runtime.conn, replace(found, extra={**found.extra, **extra}), world.clock)


def test_the_context_comes_from_the_store_and_the_handoffs(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[Any] = []
    monkeypatch.setattr(context_module, "match", lambda layout, paths, **limits: asked.append((paths, limits)) or [])
    notes_file.save(world.runtime.workspace, CodeNotes("0018", world.repo.base, files=["src/orders.py"]))
    _save(world, "implement.prepare", {"worktree": str(world.repo.path), "baseCommit": world.repo.base})
    risk = {"high": True, "reasons": ["schema：含数据库迁移"], "highRiskPaths": ["migrations/001.sql"]}
    _save(world, "implement.design", {"files": [{"path": "src/orders.py"}, {"path": "migrations/001.sql"}],
                                      "risk": risk}, minutes=1)
    found = load(world.runtime, "0018")
    assert found.worktree == world.repo.path and found.git is not None
    # 没有 origin 时取准备记下的基准
    assert found.base_commit == world.repo.base
    assert set(found.latest) == {"implement.prepare", "implement.design"} and found.round == 1
    assert found.risk == Risk(True, ("schema：含数据库迁移",), ("migrations/001.sql",))
    # 知识条目按代码笔记与方案的文件命中，条数与 token 取 controls.implement
    assert asked == [(["src/orders.py", "migrations/001.sql"], {"limit_entries": 5, "limit_tokens": 3000})]


def test_decisions_come_from_the_gates_and_from_approvals_after_a_stop(world: Any) -> None:
    started = _save(world, "implement.prepare", {})
    later = format_iso(world.clock.now() + timedelta(minutes=5))
    gate = {"point": "design", "step": "implement.approve", "verdict": "reject", "option": None, "note": "不要改接口",
            "at": later}
    recorded = {"point": "implement.design", "verdict": "approve", "option": None, "note": None,
                "at": format_iso(world.clock.now() + timedelta(minutes=2))}
    history = [
        {"at": format_iso(world.clock.now() - timedelta(days=1)), "event": "approve", "actor": "user", "reason": None,
         "note": "放行"},  # 实施开始之前的放行不算
        {"at": format_iso(world.clock.now() + timedelta(minutes=3)), "event": "start", "actor": "tightrein",
         "reason": None, "note": None},
        {"at": format_iso(world.clock.now() + timedelta(minutes=4)), "event": "approve", "actor": "user",
         "reason": None, "note": "补上测试库"},
    ]
    _update(world, **{DECISIONS: [gate, recorded], "history": history})
    found = load(world.runtime, "0018").decisions
    assert started.created_at is not None
    assert found == [
        Decision("implement.design", "approve", None, None, recorded["at"]),
        Decision("implement", "approve", None, "补上测试库", history[2]["at"]),
        Decision("implement.approve", "reject", None, "不要改接口", later),  # 关卡上的决定按记下的步骤
    ]


def test_without_any_step_the_context_has_no_worktree(world: Any) -> None:
    found = load(world.runtime, "0018")
    assert found.worktree is None and found.git is None and found.base_commit is None
    assert found.knowledge == [] and found.decisions == [] and found.notes is None
    with pytest.raises(LookupError):
        load(world.runtime, "0099")
