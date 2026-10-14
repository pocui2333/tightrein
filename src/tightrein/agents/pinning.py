"""调用点的模型固定：实际跑下来证明配置的模型不够用的调用点，以后固定改用更强的一档(按项目记在 state 表)。

- 什么时候固定：一次调用成功但用了超过 limits.escalation.pinTurns(缺省 10)轮——固定到升档阶梯上比它强的下一档；
  调用中因超时、轮数到限或格式不符升了档并且成功——固定到成功的那一档；
- 怎么用：之后这个调用点的每次调用，配置的模型在阶梯上比固定的弱时换成固定的(只升不降；不在阶梯上的模型，如按
  条件选的 fable，不受影响)；盲审调用点升档时已跳过它要独立于的模型(Settings.stronger)；
- 回放(整体测试)不固定，保证录制可重放；固定的记录在 state 表 `agents.pinned`：{调用点: {model, reason, at}}，
  要撤销就删掉那一项。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any

from tightrein.agents.params import CallParams, Model
from tightrein.protocol.naming import Clock, format_iso
from tightrein.settings.load import Settings
from tightrein.store.tables import state

KEY = "agents.pinned"


def apply(params: CallParams, conn: sqlite3.Connection, settings: Settings) -> CallParams:
    """固定的模型比配置的强时换上；fallback 不变。"""
    found = pinned(conn, settings, params.point)
    if found is None or not _stronger(settings, found, params.model):
        return params
    return replace(params, model=found)


def pinned(conn: sqlite3.Connection, settings: Settings, point: str) -> Model | None:
    item = (state.get(conn, KEY) or {}).get(point)
    if not isinstance(item, dict) or not item.get("model"):
        return None
    return settings.model(str(item["model"]))


def learn(conn: sqlite3.Connection, clock: Clock, settings: Settings, point: str, used: Model, *, turns: int,
          escalated: bool) -> str | None:
    """按这次成功的调用决定要不要固定；固定了返回说明(写进事件)，否则 None。"""
    target: Model | None
    if escalated:
        target, reason = used, f"调用中升档到 {used.alias} 才成功"
    elif turns > int(settings.get("limits.escalation.pinTurns")):
        target, reason = settings.stronger(point, used), f"{used.alias} 用了 {turns} 轮"
    else:
        return None
    current = pinned(conn, settings, point)
    if target is None or (current is not None and not _stronger(settings, target, current)):
        return None
    table: dict[str, Any] = dict(state.get(conn, KEY) or {})
    table[point] = {"model": target.alias, "reason": reason, "at": format_iso(clock.now())}
    state.put(conn, KEY, table, clock)
    return f"{point} 以后固定用 {target.alias}：{reason}"


def _stronger(settings: Settings, left: Model, right: Model) -> bool:
    """left 在升档阶梯上比 right 强；任一不在阶梯上都为否。"""
    ladder = list(settings.get("limits.escalation.ladder"))
    if left.alias not in ladder or right.alias not in ladder:
        return False
    return ladder.index(left.alias) > ladder.index(right.alias)
