"""期望断言的求值与交接文档内的路径(architecture/03 2.3.2)。

路径是 outputs 内的点分路径，`[*]` 表示数组的任一元素，`[n]` 表示第 n 个元素(从 0 开始)。
断言对路径取到的全部值求值，任一值满足即通过(数组用 [*] 表示任一元素)：
equals 相等；in 属于给定列表；contains 字符串或数组包含给定值；matches 字符串匹配正则；exists 至少取到一个值；
absent 一个值也取不到。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from tightrein.domain.enums import ScoreResult
from tightrein.evaluation.cases import Assertion
from tightrein.evaluation.scorers.base import ScoreOutcome

PATH = re.compile(r"^[^.\[\]]+(\.[^.\[\]]+|\[(\*|\d+)\])*$")
SEGMENT = re.compile(r"([^.\[\]]+)|\[(\*|\d+)\]")


def steps(path: str) -> list[str]:
    if not PATH.match(path):
        raise ValueError(f"路径写法不合格：{path}")
    return [key or index for key, index in SEGMENT.findall(path)]


def values_at(data: Any, path: str) -> list[tuple[str, Any]]:
    """路径取到的全部值，以及每个值的具体位置(`[*]` 展开为下标)。"""
    found: list[tuple[str, Any]] = [("", data)]
    for step in steps(path):
        following: list[tuple[str, Any]] = []
        for where, value in found:
            if step == "*" and isinstance(value, list):
                following += [(f"{where}[{index}]", item) for index, item in enumerate(value)]
            elif step.isdigit() and isinstance(value, list) and int(step) < len(value):
                following.append((f"{where}[{step}]", value[int(step)]))
            elif isinstance(value, Mapping) and step in value:
                following.append((f"{where}.{step}" if where else step, value[step]))
        found = following
    return found


def _satisfies(op: str, found: Any, expected: Any) -> bool:
    if op == "equals":
        return found == expected
    if op == "in":
        return isinstance(expected, list) and found in expected
    if op == "contains":
        if isinstance(found, str):
            return isinstance(expected, str) and expected in found  # 非字符串在字符串中查找会抛 TypeError
        return isinstance(found, list) and expected in found
    if op == "matches":
        return isinstance(found, str) and re.search(str(expected), found) is not None
    raise ValueError(f"不认识的断言运算：{op}")


def holds(op: str, path: str, value: Any, outputs: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """断言是否成立，以及满足条件的值的位置。"""
    found = values_at(outputs, path)
    if op == "exists":
        return bool(found), [where for where, _ in found]
    if op == "absent":
        return not found, []
    matched = [where for where, item in found if _satisfies(op, item, value)]
    return bool(matched), matched


def evaluate(assertion: Assertion, outputs: Mapping[str, Any]) -> ScoreOutcome:
    ok, where = holds(assertion.op, assertion.path, assertion.value, outputs)
    if ok:
        return ScoreOutcome(ScoreResult.PASS, assertion.description, tuple(f"outputs.{item}" for item in where))
    shown = [item for _, item in values_at(outputs, assertion.path)]
    detail = f"{assertion.path} 取到 {shown!r}" if shown else f"{assertion.path} 不存在"
    return ScoreOutcome(ScoreResult.FAIL, f"{assertion.description}：不成立，{detail}")
