"""问题转主张：只放观察到的事实，不放采集时的推测与任何已有判定。

- 一句话主张按问题的检查类别套模板写(STATEMENTS)，不给模型带倾向的判断；
- 事实带序号(出现次数、首末出现时间与版本、信号样本、主干差异)，判「不成立」时可以引用第几条；
- 信号样本按(角色, 位置)去重，从最近的取，有条数上限：同一处重复刷屏不会挤掉其他角色或位置的样本；
  样本去掉采集时已有的判定(verification、verdict)；
- 入口取路由、堆栈首个本项目帧与静态位置，去重保序；
- 用户补充的信息标「用户提供」，跨次累积。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from tightrein.store.tables.occurrences import Occurrence
from tightrein.store.tables.problems import Problem

JUDGEMENT_KEYS = frozenset({"verdict", "verification"})
USER_PROVIDED = "用户提供"
NO_ENTRY = "(没有可直接得到的入口)"
ROUTE_SOURCES = frozenset({"collect.api_fuzz", "collect.access_log"})
CODE_SOURCES = frozenset({"collect.static", "collect.incidental"})
# 检查类别 → 主张模板；{role}、{location}、{message}、{status} 由程序填入
STATEMENTS: dict[str, str] = {
    "authz": "{role} 不具备访问权限，调用 {location} 却返回了 {status}",
    "server_error": "{role} 调用 {location} 时服务端返回 {status}：{message}",
    "latency": "{location} 的响应耗时超出预期：{message}",
    "error_rate": "{location} 的错误率超出预期：{message}",
    "error": "运行时抛出异常：{message}",
    "alert": "业务告警 {location} 已触发：{message}",
    "probe": "项目探针在 {location} 发现：{message}",
    "static": "{location} 存在以下问题：{message}",
}
DEFAULT_STATEMENT = "{location} 存在以下问题：{message}"
ANONYMOUS = "未登录用户"


@dataclass(frozen=True)
class Fact:
    label: str
    value: Any

    def to_json(self) -> dict[str, Any]:
        return {"label": self.label, "value": self.value}


@dataclass(frozen=True)
class Claim:
    statement: str
    title: str
    facts: tuple[Fact, ...]
    entry_points: tuple[str, ...]
    user_notes: tuple[str, ...] = ()

    def with_fact(self, label: str, value: Any) -> Claim:
        return replace(self, facts=(*self.facts, Fact(label, value)))

    def to_json(self) -> dict[str, Any]:
        return {"statement": self.statement, "title": self.title, "facts": [fact.to_json() for fact in self.facts],
                "entryPoints": list(self.entry_points), "userNotes": list(self.user_notes)}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Claim:
        return cls(data["statement"], data["title"], tuple(Fact(item["label"], item["value"]) for item in data["facts"]),
                   tuple(data["entryPoints"]), tuple(data.get("userNotes") or ()))

    def render(self) -> str:
        """放进提示「本次输入」的一段：主张、编号的事实、入口与用户提供的信息。"""
        lines = ["### 主张", "", self.statement, "", "### 事实", ""]
        for number, fact in enumerate(self.facts, start=1):
            value = fact.value if isinstance(fact.value, str) else json.dumps(fact.value, ensure_ascii=False)
            lines.append(f"{number}. {fact.label}：{value}")
        lines += ["", "### 入口", "", *(f"- {entry}" for entry in self.entry_points or (NO_ENTRY,))]
        if self.user_notes:
            lines += ["", f"### {USER_PROVIDED}的信息", "", *(f"- {note}({USER_PROVIDED})" for note in self.user_notes)]
        return "\n".join(lines)


def build(problem: Problem, found: Sequence[Occurrence], user_notes: Sequence[str] = (),
          sample_limit: int = 5) -> Claim:
    if not found:
        raise ValueError(f"问题 {problem.id} 没有出现记录，无法组装主张")
    latest = max(found, key=lambda item: (item.seen_at, item.id or 0))
    first = min(found, key=lambda item: (item.seen_at, item.id or 0))
    facts = [
        Fact("出现次数", problem.count),
        Fact("首次出现", f"{first.seen_at.isoformat()}(版本 {first.commit or '未知'})"),
        Fact("末次出现", f"{latest.seen_at.isoformat()}(版本 {latest.commit or problem.last_commit or '未知'})"),
        *(Fact(f"信号 {sample_id(item)}", sample(item)) for item in samples(found, sample_limit)),
    ]
    return Claim(statement(problem, latest), problem.title, tuple(facts), entry_points(problem, found),
                 tuple(user_notes))


def statement(problem: Problem, latest: Occurrence) -> str:
    evidence = signal_evidence(latest)
    template = STATEMENTS.get(check_kind(problem, latest), DEFAULT_STATEMENT)
    return template.format(role=evidence.get("role") or ANONYMOUS, location=problem.location or "(位置未知)",
                           message=latest.evidence.get("message") or problem.title,
                           status=evidence.get("status", "未知状态"))


def check_kind(problem: Problem, latest: Occurrence) -> str:
    """模板的键：越权(预估 P0 的接口问题)单独一类，其余取检查类别(incidental:<类别> 取前缀)。"""
    if problem.source == "collect.api_fuzz" and severity_hint(latest) == "P0":
        return "authz"
    return problem.check_type.partition(":")[0]


def samples(found: Sequence[Occurrence], limit: int) -> list[Occurrence]:
    """从最近的开始，按(角色, 位置)去重，最多 limit 条。"""
    chosen: list[Occurrence] = []
    seen: set[tuple[str | None, str | None]] = set()
    for item in sorted(found, key=lambda occurrence: (occurrence.seen_at, occurrence.id or 0), reverse=True):
        key = (signal_evidence(item).get("role"), item.evidence.get("location"))
        if key not in seen:
            seen.add(key)
            chosen.append(item)
        if len(chosen) == limit:
            break
    return chosen


def sample(item: Occurrence) -> dict[str, Any]:
    evidence = {key: value for key, value in signal_evidence(item).items() if key not in JUDGEMENT_KEYS}
    return {"checkType": item.evidence.get("checkType"), "location": item.evidence.get("location"),
            "symbol": item.evidence.get("symbol"), "message": item.evidence.get("message"),
            "seenAt": item.seen_at.isoformat(), "commit": item.commit, "evidence": evidence}


def sample_id(item: Occurrence) -> str:
    return str(item.evidence.get("signal") or item.id)


def entry_points(problem: Problem, found: Sequence[Occurrence]) -> tuple[str, ...]:
    entries: list[str] = []
    if problem.source in ROUTE_SOURCES and problem.location:
        entries.append(problem.location)
    for item in found:
        frames = [frame for frame in frames_of(item) if frame.get("file") and frame.get("line")]
        if frames:
            entries.append(f"{frames[0]['file']}:{frames[0]['line']}")
        location = item.evidence.get("location")
        if problem.source in CODE_SOURCES and isinstance(location, str) and ":" in location:
            entries.append(location)
    return tuple(dict.fromkeys(entries))


def frames_of(item: Occurrence) -> list[dict[str, Any]]:
    """信号中本项目的堆栈帧(采集写在来源证据的 projectFrames)。"""
    return [frame for frame in signal_evidence(item).get("projectFrames") or [] if isinstance(frame, dict)]


def severity_hint(item: Occurrence) -> str | None:
    """信号的严重度提示(Signal.severity_hint)：collect/dedup/changes.signal_record 把它写在出现记录 evidence 的顶层
    severityHint，与来源自己的证据(evidence.evidence)分开。"""
    value = item.evidence.get("severityHint")
    return str(value) if value else None


def signal_evidence(item: Occurrence) -> dict[str, Any]:
    """出现记录里来源给的证据(collect/dedup 写在 evidence.evidence)。"""
    value = item.evidence.get("evidence")
    return dict(value) if isinstance(value, Mapping) else {}
