"""复现确认：待确认(pending)的问题按策略转为待评估(new)、观察中(watching)或间歇(intermittent)。

- 需要重放的只有配置在 controls."collect.dedup".replayChecks 中的「来源 → 检查类型」(缺省只有 API 模糊测试的 5xx)：
  重放器只凭状态码判断，其他检查重放了也判断不了；来源已确认可复现(Signal.reproducible)的不再重放；
- 重放每个问题 replayAttempts 次：任一次复现即有效；有「无法判断」(没有记录请求、登录失败、目标不可用)且没有复现时
  保持待确认，下次重试，不能当成「未复现」；全部未复现才标为间歇，之后不再重放，再出现时由状态更新转为 new；
- 没有重放器(来源模块没有 replay.py)时，需要重放的问题保持待确认；
- 其余来源首次出现即有效：确定性来源(静态巡检、API 模糊测试的 5xx 等)与已由模型取证成立的直接转为 new；
  运行时的偶发问题先观察(watching)，累计出现到 watchOccurrences 次才转为 new；
- 遗留的待确认问题(上次重放无法判断)按当前策略重新判定，用本次或数据库中最近一次出现的证据。
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tightrein.collect.common.signals import Signal
from tightrein.collect.dedup.changes import ChangeSet, Reproduction
from tightrein.collect.dedup.status import Event, ProblemStatus
from tightrein.store.tables.problems import Problem

REPLAY = "replay"
IMMEDIATE = "immediate"
REPRODUCED = "reproduced"
NOT_REPRODUCED = "not_reproduced"
UNAVAILABLE = "unavailable"
REPLAY_MODULE = "tightrein.{source}.replay"  # 来源模块自带的重放器：replay(runtime, evidence) -> 三值之一

Replayer = Callable[[Mapping[str, Any]], str]


@dataclass(frozen=True)
class Params:
    replay_checks: Mapping[str, frozenset[str]]  # 来源 → 需要重放确认的检查类型
    attempts: int
    watch_occurrences: int


@dataclass(frozen=True)
class Sighting:
    """判定所需的一次出现：本次的信号，或数据库中最近一次出现的记录(occurrences.evidence)。"""

    evidence: Mapping[str, Any]
    deterministic: bool
    verified: bool
    reproducible: bool

    @classmethod
    def of_signal(cls, signal: Signal) -> Sighting:
        return cls(signal.evidence, signal.deterministic, signal.verified, signal.reproducible)

    @classmethod
    def of_record(cls, record: Mapping[str, Any]) -> Sighting:
        return cls(record.get("evidence") or {}, bool(record.get("deterministic")), bool(record.get("verified")),
                   bool(record.get("reproducible")))


def strategy(problem: Problem, sightings: Sequence[Sighting], params: Params) -> str:
    if problem.check_type not in params.replay_checks.get(problem.source, frozenset()):
        return IMMEDIATE
    return IMMEDIATE if any(item.reproducible for item in sightings) else REPLAY


def judge(results: Sequence[str]) -> Event | None:
    """任一次复现即有效；有无法判断且没有复现时为 None(保持待确认)；全部未复现为间歇。"""
    if REPRODUCED in results:
        return Event.CONFIRMED
    if UNAVAILABLE in results or not results:
        return None
    return Event.NOT_REPRODUCED


def replayer_for(source: str, bind: Callable[[Callable[..., str]], Replayer]) -> Replayer | None:
    """来源模块的重放器；没有时为 None。bind 把运行时上下文绑定进去(重放器签名为 replay(runtime, evidence))。"""
    name = REPLAY_MODULE.format(source=source)
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name is None or not name.startswith(error.name):
            raise  # 重放器自己缺依赖，不能当成「没有重放器」
        return None
    found = getattr(module, "replay", None)
    return bind(found) if callable(found) else None


def apply(changeset: ChangeSet, stored: Sequence[Problem], recorded: Mapping[str, Mapping[str, Any]],
          replayers: Callable[[str], Replayer | None], params: Params) -> None:
    """stored 为数据库中遗留的待确认问题；recorded 为它们最近一次出现的记录(本次没有出现时用)。"""
    for problem_id in list(changeset.created):
        problem = changeset.known[problem_id]
        if problem.status == ProblemStatus.PENDING:
            sightings = [Sighting.of_signal(signal) for signal in changeset.occurred[problem_id]]
            _confirm(changeset, problem, sightings, replayers, params)
    for found in stored:
        problem = changeset.known.get(found.id, found)
        if problem.id in changeset.created or problem.status != ProblemStatus.PENDING:
            continue
        seen = changeset.occurred.get(problem.id)
        if seen:
            sightings = [Sighting.of_signal(seen[-1])]
        elif problem.id in recorded:
            sightings = [Sighting.of_record(recorded[problem.id])]
        else:
            continue
        _confirm(changeset, problem, sightings, replayers, params)


def _confirm(changeset: ChangeSet, problem: Problem, sightings: Sequence[Sighting],
             replayers: Callable[[str], Replayer | None], params: Params) -> None:
    if strategy(problem, sightings, params) == REPLAY:
        _replay(changeset, problem, sightings[-1], replayers(problem.source), params.attempts)
        return
    changeset.reproduction[problem.id] = Reproduction(IMMEDIATE)
    if any(item.deterministic or item.verified for item in sightings) or problem.count >= params.watch_occurrences:
        changeset.transition(problem, Event.CONFIRMED)
    else:
        changeset.transition(problem, Event.WATCH, occurrences=problem.count)


def _replay(changeset: ChangeSet, problem: Problem, sighting: Sighting, replayer: Replayer | None,
            attempts: int) -> None:
    if replayer is None:
        changeset.reproduction[problem.id] = Reproduction(REPLAY)
        changeset.notes.append(f"{problem.id}：{problem.source} 没有重放器，保持待确认")
        return
    results = [replayer(sighting.evidence) for _ in range(attempts)]
    event = judge(results)
    reproduced = REPRODUCED if event is Event.CONFIRMED else NOT_REPRODUCED if event is not None else UNAVAILABLE
    changeset.reproduction[problem.id] = Reproduction(REPLAY, len(results), reproduced)
    if event is not None:
        changeset.transition(problem, event, replays=results)
