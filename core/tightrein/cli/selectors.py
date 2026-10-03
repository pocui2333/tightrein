"""选择器与上下游链(design 15.3)。对象与状态选择器的解析在 orchestrator/resume.select。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from tightrein.cli.exit_codes import UsageError
from tightrein.domain.enums import Stage
from tightrein.orchestrator import resume

CHAIN_ORDER = (Stage.COLLECT, Stage.AGGREGATE, Stage.TRIAGE, Stage.ISSUE)


@dataclass(frozen=True)
class Chain:
    module: Stage
    upstream: bool = False
    downstream: bool = False


def parse_chain(text: str) -> Chain:
    """`triage+` 连同下游，`+aggregate` 连同上游，`triage` 只有它自己。"""
    name = text.strip("+")
    module = next((stage for stage in CHAIN_ORDER if stage.value == name), None)
    if module is None:
        raise UsageError(f"链只能由 {'、'.join(stage.value for stage in CHAIN_ORDER)} 组成：{text}")
    return Chain(module, text.startswith("+"), text.endswith("+"))


def modules_of(chain: Chain) -> tuple[Stage, ...]:
    index = CHAIN_ORDER.index(chain.module)
    start = 0 if chain.upstream else index
    end = len(CHAIN_ORDER) if chain.downstream else index + 1
    return CHAIN_ORDER[start:end]


def problem_ids(conn: sqlite3.Connection, selectors: Sequence[str]) -> tuple[str, ...]:
    """triage 与 issue 的 --select：问题编号或 status:、run:、probe: 选中的问题。"""
    found = resume.select(conn, selectors)
    others = [ref.id for ref in found if ref.kind != resume.PROBLEM]
    if others:
        raise UsageError(f"这里只能选择问题：{'、'.join(others)}")
    return tuple(ref.id for ref in found)
