"""访问日志行的解析：JSON 行按 sources.access-log.fields 取字段(嵌套以 . 连接)，或按 sources.access-log.pattern 的
命名分组(method、route、status、durationMs)。取不到方法、路由或状态码的行计入无法解析；耗时可以没有。
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


def _field(record: Mapping[str, Any], path: str) -> Any:
    value: Any = record
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _request(values: Mapping[str, Any]) -> Request | None:
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


def parse(lines: Iterable[str], fields: Mapping[str, str], pattern: str | None) -> tuple[list[Request], int]:
    """返回 (请求, 无法解析的行数)。"""
    compiled = re.compile(pattern) if pattern else None
    found: list[Request] = []
    unparsed = 0
    for line in lines:
        if not line.strip():
            continue
        values: Mapping[str, Any] | None = None
        if compiled is not None:
            match = compiled.search(line)
            values = match.groupdict() if match else None
        else:
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            if isinstance(record, dict):
                values = {name: _field(record, fields[name]) for name in FIELDS if name in fields}
        request = _request(values) if values is not None else None
        if request is None:
            unparsed += 1
        else:
            found.append(request)
    return found, unparsed
