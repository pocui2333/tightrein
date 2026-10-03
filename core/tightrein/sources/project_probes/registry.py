"""项目探针的登记与到期判断：sources.project-probes 中每项一个探针(name、command、every、keychain、timeoutSeconds)。

every 写成 <数字><单位>(m 分钟、h 小时、d 天)；从未运行过或距上次运行已达到间隔即到期。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.store.repos import probe_states

SETTINGS = "sources.project-probes"
INTERVAL = re.compile(r"^(?P<count>[1-9][0-9]*)(?P<unit>[mhd])$")
UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def interval(text: str) -> timedelta:
    found = INTERVAL.match(text)
    if found is None:
        raise ValueError(f"间隔应写成 <数字>m|h|d：{text}")
    return timedelta(**{UNITS[found.group("unit")]: int(found.group("count"))})


@dataclass(frozen=True)
class Registration:
    name: str
    command: tuple[str, ...]
    every: timedelta
    keychain: tuple[str, ...] = ()
    timeout_seconds: float | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Registration:
        timeout = data.get("timeoutSeconds")
        return cls(data["name"], tuple(data["command"]), interval(data["every"]), tuple(data.get("keychain", [])),
                   None if timeout is None else float(timeout))


def registered(config: ProjectConfig) -> list[Registration]:
    return [Registration.from_dict(item) for item in config.data.get("sources", {}).get("project-probes", [])]


def find(config: ProjectConfig, name: str) -> Registration:
    found = next((item for item in registered(config) if item.name == name), None)
    if found is None:
        raise LookupError(f"{SETTINGS} 中没有登记探针 {name}")
    return found


def due(conn: sqlite3.Connection, items: Sequence[Registration], now: datetime) -> list[Registration]:
    result = []
    for item in items:
        state = probe_states.get(conn, item.name)
        if state is None or now - state.last_run_at >= item.every:
            result.append(item)
    return result
