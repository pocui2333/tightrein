"""主张与确定性工具的结果，以及取证前的筛选与截取。

取证是采集最贵的一步(每条主张单独一次模型调用)，所以取证前先用程序挡掉不值得取证的：
- 没有位置(文件不在检查范围的 worktree 中、行号越界)或没有触发条件的：取证也说不清，直接丢；
- 与未关闭的问题重复(同一文件、行号相近)：问题已在评估或修复流程里，不再取证一次；
- 命中抑制规则(以前判为误报，规则在去重的 suppress 中)：按去重同样的指纹与规则判断；
- 同一处(文件、行、规则)只取证一次。
命中已接受取舍的由审查写进 excluded，不成为主张。

截取：增量审查最多提 N 条(controls."collect.static.review".maxClaims)，程序再按严重度兜底取前 N 条，迫使只报最重要的。
取证名额：先取证上次超出上限遗留的，再按严重度取证本次的高、中级；低级与超出上限的留在待取证清单(state 表)，
不丢弃，下次运行接着取证。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from tightrein.collect.common.signals import Signal
from tightrein.collect.dedup import group, normalize, suppress
from tightrein.collect.dedup.suppress import SuppressionRule
from tightrein.store.tables import problems

SOURCE = "collect.static"
CHECK_TYPE = "static"
SEVERITIES = ("high", "medium", "low")  # 从高到低
LOW = "low"
CLOSED_STATUSES = frozenset({"resolved", "closed"})
NO_LOCATION = "noLocation"
NO_TRIGGER = "noTrigger"
DUPLICATE = "duplicate"
SUPPRESSED = "suppressed"
REPEATED = "repeated"
OVER_LIMIT = "overLimit"
VULNERABILITY = "vulnerability"


@dataclass(frozen=True)
class ToolFinding:
    tool: str
    kind: str  # lint、build-warning、vulnerability
    rule: str
    file: str
    line: int | None
    column: int | None
    message: str
    severity: str
    package: Mapping[str, Any] | None = None

    @property
    def vulnerability(self) -> bool:
        return self.kind == VULNERABILITY

    @classmethod
    def from_json(cls, item: Mapping[str, Any]) -> ToolFinding:
        return cls(item["tool"], item["kind"], item["rule"], item["file"], item.get("line"), item.get("column"),
                   item["message"], item["severity"], item.get("package"))

    def to_json(self) -> dict[str, Any]:
        return {"tool": self.tool, "kind": self.kind, "rule": self.rule, "file": self.file, "line": self.line,
                "column": self.column, "message": self.message, "severity": self.severity,
                "package": None if self.package is None else dict(self.package)}


@dataclass(frozen=True)
class Claim:
    file: str
    line: int
    rule_or_pattern: str
    layer: str  # deterministic、incremental、full、baseline
    severity: str
    statement: str
    trigger: str

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Claim:
        return cls(data["file"], int(data["line"]), data["ruleOrPattern"], data["layer"],
                   data.get("severity") or LOW, data["statement"], data["trigger"])

    def to_json(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "ruleOrPattern": self.rule_or_pattern, "layer": self.layer,
                "severity": self.severity, "statement": self.statement, "trigger": self.trigger}

    @property
    def key(self) -> tuple[str, int, str]:
        return self.file, self.line, self.rule_or_pattern

    @property
    def severity_rank(self) -> int:
        """高为 0；没有标注的排在最后。"""
        return SEVERITIES.index(self.severity) if self.severity in SEVERITIES else len(SEVERITIES)


@dataclass
class Screened:
    kept: list[Claim] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)  # 原因 → 条数

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1


@dataclass(frozen=True)
class Selection:
    verify: list[Claim]
    pending: list[dict[str, Any]]  # 留到下次的：claim 与原因(low、overLimit)


@dataclass(frozen=True)
class OpenProblem:
    file: str
    line: int | None


def open_problems(conn: sqlite3.Connection) -> list[OpenProblem]:
    """未关闭的静态巡检问题的位置；一次读出，之后只在内存中比对。"""
    found = []
    for problem in problems.find(conn):
        if problem.source != SOURCE or problem.status in CLOSED_STATUSES or problem.location is None:
            continue
        line = problem.extra.get(group.LINE)
        found.append(OpenProblem(problem.location.partition(":")[0], int(line) if line is not None else None))
    return found


def screen(claims: Iterable[Claim], *, worktree: Path, known: Sequence[OpenProblem],
           rules: Sequence[SuppressionRule], now: datetime, nearby_lines: int, run: str) -> Screened:
    result = Screened()
    seen: set[tuple[str, int, str]] = set()
    lengths: dict[str, int | None] = {}
    by_file: dict[str, list[OpenProblem]] = {}
    for item in known:
        by_file.setdefault(item.file, []).append(item)
    for claim in claims:
        if claim.key in seen:
            result.drop(REPEATED)
            continue
        seen.add(claim.key)
        if claim.file not in lengths:
            lengths[claim.file] = _line_count(worktree, claim.file)
        length = lengths[claim.file]
        if length is None or not 1 <= claim.line <= length:
            result.drop(NO_LOCATION)
        elif not claim.trigger.strip():
            result.drop(NO_TRIGGER)
        elif any(item.line is not None and abs(item.line - claim.line) <= nearby_lines
                 for item in by_file.get(claim.file, ())):
            result.drop(DUPLICATE)
        elif _suppressed(claim, rules, now, run):
            result.drop(SUPPRESSED)
        else:
            result.kept.append(claim)
    return result


def top(claims: Sequence[Claim], limit: int) -> tuple[list[Claim], int]:
    """按严重度(同级保持原顺序)取前 limit 条；返回保留的与截掉的条数。"""
    ordered = sorted(claims, key=lambda claim: claim.severity_rank)
    return ordered[:limit], max(0, len(ordered) - limit)


def select(queued: Sequence[Claim], fresh: Sequence[Claim], limit: int) -> Selection:
    """先取证遗留的，再按严重度取证本次的高、中级，合计不超过 limit；其余留到下次。"""
    ordered = sorted(fresh, key=lambda claim: claim.severity_rank)
    pending = [pending_item(claim, LOW) for claim in ordered if claim.severity == LOW]
    candidates = [*queued, *(claim for claim in ordered if claim.severity != LOW)]
    pending += [pending_item(claim, OVER_LIMIT) for claim in candidates[limit:]]
    return Selection(list(candidates[:limit]), pending)


def pending_item(claim: Claim, reason: str) -> dict[str, Any]:
    return {"claim": claim.to_json(), "reason": reason}


def queued_claims(items: Iterable[Mapping[str, Any]]) -> tuple[list[Claim], list[dict[str, Any]]]:
    """待取证清单：超出上限遗留的排队取证；低级的继续留着(选中时才取证)。"""
    queued, kept = [], []
    for item in items:
        if item.get("reason") == OVER_LIMIT:
            queued.append(Claim.from_json(item["claim"]))
        else:
            kept.append(dict(item))
    return queued, kept


def _line_count(worktree: Path, relative: str) -> int | None:
    path = worktree / relative
    try:
        if not path.resolve().is_relative_to(worktree.resolve()) or not path.is_file():
            return None
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return None


def _suppressed(claim: Claim, rules: Sequence[SuppressionRule], now: datetime, run: str) -> bool:
    """按去重的指纹算法与抑制规则判断：先拼一条临时信号，再取同样的指纹。"""
    if not rules:
        return False
    signal = Signal(id="S-CLAIM", run=run, source=SOURCE, check_type=CHECK_TYPE, location=f"{claim.file}:{claim.line}",
                    symbol=None, message=claim.statement, evidence={"rule": claim.rule_or_pattern}, occurred_at="",
                    commit=None, environment=None, severity_hint=None, group_key=None, deterministic=True,
                    verified=False, reproducible=False)
    fingerprint = group.fingerprint(signal, normalize.message(claim.statement))
    return suppress.match(signal, fingerprint, rules, now) is not None
