"""第三方 skill 的每月核实(design 9.11)：读取 third_party/skills.lock.yaml，经注入的只读查询取来源仓库的星标数、
最近提交日期与是否归档，不再满足门槛的列入周报，由用户决定替换或自行实现。

- 锁定清单不存在时跳过；每月一次，以本机日期的年月为幂等键，结果存在键中，本月再次生成周报时直接复用；
- 有查询失败时不记录本月结果，下次生成周报时重查；
- 不改写锁定清单。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta, tzinfo
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock, local_date
from tightrein.store import idempotency
from tightrein.store.files import yaml_text


@dataclass(frozen=True)
class RepoFacts:
    stars: int
    last_commit: date
    archived: bool
    license: str | None = None


RepoQuery = Callable[[str], RepoFacts]


@dataclass(frozen=True)
class ThirdPartyCheck:
    name: str
    source: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "source": self.source, "passed": self.passed, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThirdPartyCheck:
        return cls(data["name"], data["source"], data["passed"], data["detail"])


def month_key(today: date) -> str:
    return f"third-party:{today:%Y-%m}"


def judge(name: str, source: str, facts: RepoFacts, config: ProjectConfig, today: date) -> ThirdPartyCheck:
    min_stars = config.whole_threshold("learn.thirdPartyMinStars")
    days = config.whole_threshold("learn.thirdPartyCommitDays")
    reasons = []
    if facts.stars < min_stars:
        reasons.append(f"星标数 {facts.stars} 低于 {min_stars}")
    if facts.last_commit < today - timedelta(days=days):
        reasons.append(f"最近提交 {facts.last_commit.isoformat()} 已超过 {days} 天")
    if facts.archived:
        reasons.append("来源仓库已归档")
    detail = "；".join(reasons) or f"星标数 {facts.stars}，最近提交 {facts.last_commit.isoformat()}"
    return ThirdPartyCheck(name, source, not reasons, detail)


def check(conn: sqlite3.Connection, clock: Clock, config: ProjectConfig, lock_path: Path, query: RepoQuery,
          zone: tzinfo | None, *, record: bool = True) -> list[ThirdPartyCheck]:
    if not lock_path.is_file():
        return []
    today = local_date(clock.now(), zone)
    saved = idempotency.get(conn, month_key(today))
    if saved is not None and saved.status == idempotency.DONE and saved.result is not None:
        return [ThirdPartyCheck.from_dict(item) for item in saved.result["checks"]]
    skills = (yaml_text.load(lock_path.read_text(encoding="utf-8")) or {}).get("skills", [])
    found, failed = [], False
    for skill in skills:
        try:
            facts = query(skill["source"])
        except LookupError as error:
            failed = True
            found.append(ThirdPartyCheck(skill["name"], skill["source"], False, f"只读查询失败：{error}"))
            continue
        found.append(judge(skill["name"], skill["source"], facts, config, today))
    if record and not failed:
        idempotency.run_once(conn, month_key(today), lambda: {"checks": [item.to_dict() for item in found]}, clock)
    return found
