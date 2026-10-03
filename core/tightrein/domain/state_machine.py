"""状态机的公共部分(architecture/01 2.3)：转换规则写成数据，由 resolve 查表。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, Iterable, TypeVar

from tightrein.domain.enums import LabeledEnum

StateT = TypeVar("StateT", bound=LabeledEnum)
EventT = TypeVar("EventT", bound=LabeledEnum)

Conditions = tuple[tuple[str, object], ...]


class InvalidTransition(Exception):
    """状态与事件的组合不在转换表中。"""

    def __init__(self, state: LabeledEnum, event: LabeledEnum) -> None:
        self.state = state
        self.event = event
        super().__init__(f"状态 {state.value}({state.label})不接受事件 {event.value}({event.label})")


@dataclass(frozen=True)
class SideEffect:
    """交给上层执行的后续动作；状态机自身不执行。"""

    kind: LabeledEnum
    detail: dict[str, Any] = field(default_factory=dict)


def conditions_hold(conditions: Conditions, subject: object) -> bool:
    """每个 (属性名, 取值) 都与 subject 上的同名属性相等。"""
    return all(getattr(subject, name) == value for name, value in conditions)


@dataclass(frozen=True)
class Rule(Generic[StateT, EventT]):
    """一行转换规则。

    target 为 None 表示状态不变；when 是对 context 的条件；payload 是写进每个副作用的固定内容；
    carry 是从 context 复制到每个副作用的字段，这些字段必须由调用方提供。
    """

    event: EventT
    sources: frozenset[StateT]
    target: StateT | None
    when: Conditions = ()
    effects: tuple[LabeledEnum, ...] = ()
    payload: Conditions = ()
    carry: tuple[str, ...] = ()

    def matches(self, state: StateT, event: EventT, context: object) -> bool:
        return event is self.event and state in self.sources and conditions_hold(self.when, context)


def resolve(
    rules: Iterable[Rule[StateT, EventT]], state: StateT, event: EventT, context: object
) -> Rule[StateT, EventT]:
    """按顺序返回第一条匹配的规则；没有匹配时抛出 InvalidTransition。"""
    for rule in rules:
        if rule.matches(state, event, context):
            return rule
    raise InvalidTransition(state, event)


def apply(
    rules: Iterable[Rule[StateT, EventT]], state: StateT, event: EventT, context: object
) -> tuple[StateT, tuple[SideEffect, ...]]:
    rule = resolve(rules, state, event, context)
    detail: dict[str, Any] = dict(rule.payload)
    for name in rule.carry:
        value = getattr(context, name)
        if value is None:
            raise ValueError(f"事件 {event.value} 需要提供 {name}")
        detail[name] = value
    target = rule.target if rule.target is not None else state
    return target, tuple(SideEffect(kind, dict(detail)) for kind in rule.effects)
