"""访问日志的行 → 请求：JSON 行按 fields 取字段(嵌套以 . 连接)，纯文本按 pattern 的命名分组(method、route、status、
durationMs)，给出 pattern 时按它。路由去掉查询串，方法转大写；取不到方法、路由或状态码的行计入无法解析，耗时可以没有。
项目脚本直接交出已解析的请求时用 request_of 校验每一条。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

FIELDS = ("method", "route", "status", "durationMs")


@dataclass(frozen=True)
class Request:
    method: str
    route: str
    status: int
    duration_ms: float | None

    @property
    def endpoint(self) -> str:
        return f"{self.method} {self.route}"


def parse(lines: Iterable[str], fields: Mapping[str, str], pattern: str | None) -> tuple[list[Request], int]:
    """返回 (请求, 无法解析的行数)；空行不计。"""
    compiled = re.compile(pattern) if pattern else None
    found: list[Request] = []
    unparsed = 0
    for line in lines:
        if not line.strip():
            continue
        values = _from_pattern(line, compiled) if compiled is not None else _from_json(line, fields)
        request = request_of(values) if values is not None else None
        if request is None:
            unparsed += 1
        else:
            found.append(request)
    return found, unparsed


def request_of(values: Mapping[str, Any]) -> Request | None:
    method, route, status = values.get("method"), values.get("route"), values.get("status")
    if not method or not route or status in (None, ""):
        return None
    try:
        code = int(status)
        duration = None if values.get("durationMs") in (None, "") else float(values["durationMs"])
    except (TypeError, ValueError):
        return None
    path = urlsplit(str(route)).path or str(route)
    return Request(str(method).upper(), path, code, duration)


def _from_pattern(line: str, pattern: re.Pattern[str]) -> dict[str, Any] | None:
    match = pattern.search(line)
    return match.groupdict() if match else None


def _from_json(line: str, fields: Mapping[str, str]) -> dict[str, Any] | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    return {name: _field(record, fields[name]) for name in FIELDS if name in fields}


def _field(record: Mapping[str, Any], path: str) -> Any:
    value: Any = record
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value
