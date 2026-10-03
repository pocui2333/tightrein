"""问题转主张(architecture/06 4.2，design 3.3)，纯函数。

主张只放观察到的事实，不放聚合阶段的推测与任何已有判定：一句话主张(按采集方法与检查套用模板)、短标题(问题标题，写现象)、
带序号的事实(出现次数、首末出现时间与版本、信号样本)、入口(路由、堆栈首帧、静态位置)与用户补充的信息。
信号样本按(角色, 位置)去重，最多 sample_limit 条，去掉静态巡检信号中已有的判定。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from tightrein.domain.clock import format_iso
from tightrein.domain.enums import Probe
from tightrein.domain.fingerprint import AUTHZ_CHECK
from tightrein.domain.problem import Problem, role_of
from tightrein.domain.signal import Signal

SERVER_ERROR_CHECK = "not_a_server_error"
SLOW_CHECK = "max_response_time"
FRONTEND_CHECK = "frontend-error"
JUDGEMENT_KEYS = frozenset({"verdict", "verification"})  # 静态巡检信号中已有的判定，不交给分诊角色
USER_PROVIDED = "用户提供"


@dataclass(frozen=True)
class Fact:
    label: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
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

    def to_dict(self) -> dict[str, Any]:
        return {"statement": self.statement, "title": self.title, "facts": [fact.to_dict() for fact in self.facts],
                "entryPoints": list(self.entry_points), "userNotes": list(self.user_notes)}

    def render(self) -> str:
        """放进执行器任务说明的一段：主张、编号的事实、入口与用户提供的信息。"""
        lines = ["## 主张", "", self.statement, "", "## 事实", ""]
        for number, fact in enumerate(self.facts, start=1):
            value = fact.value if isinstance(fact.value, str) else json.dumps(fact.value, ensure_ascii=False)
            lines.append(f"{number}. {fact.label}：{value}")
        lines += ["", "## 入口", "", *(f"- {entry}" for entry in self.entry_points or ("(没有可直接得到的入口)",))]
        if self.user_notes:
            lines += ["", f"## {USER_PROVIDED}的信息", "", *(f"- {note}({USER_PROVIDED})" for note in self.user_notes)]
        return "\n".join(lines)


def _request(signal: Signal) -> str:
    request = signal.ctx("request") or {}
    parts = [str(request.get("path") or signal.location)]
    for key in ("query", "body"):
        if request.get(key):
            parts.append(json.dumps(request[key], ensure_ascii=False))
    return " ".join(parts)


def _frames(signal: Signal) -> list[dict[str, Any]]:
    return [frame for frame in signal.ctx("projectFrames") or [] if isinstance(frame, dict)]


def statement(problem: Problem, signal: Signal) -> str:
    role = role_of(signal) or "未登录用户"
    status = signal.ctx("response", "status")
    probe, check, location = problem.probe, signal.check, problem.scope.location
    if probe is Probe.API_FUZZ and check == AUTHZ_CHECK:
        required = "、".join(signal.ctx("requiredCapabilities") or []) or "端点要求的能力"
        return f"{role} 不具备 {required}，调用 {location} 却返回了 {status}"
    if probe is Probe.API_FUZZ and check == SERVER_ERROR_CHECK:
        return f"{role} 调用 {location}，传入 {_request(signal)} 时服务端返回 {status}"
    if probe is Probe.API_FUZZ and check == SLOW_CHECK:
        return f"{location} 在 {_request(signal)} 下耗时 {signal.ctx('response', 'elapsedMs')} 毫秒"
    if probe is Probe.API_FUZZ:
        return f"{location} 的响应与接口描述不一致：{signal.message}"
    if probe is Probe.PLATFORM_ERRORS:
        kind = signal.ctx("exceptionType") or signal.ctx("category") or "异常"
        where = "浏览器端" if check == FRONTEND_CHECK else "服务端"
        return f"{where}抛出 {kind}：{signal.normalized_message or signal.message}"
    if probe is Probe.ALERTS:
        return f"业务告警 {location} 已触发：{signal.message}"
    if probe is Probe.ACCESS_LOG:
        return f"{location} 的性能或可用性退化：{signal.message}"
    if probe is Probe.PROJECT_PROBE:
        return f"项目探针 {check} 在 {location} 发现：{signal.message}"
    if probe is Probe.STATIC:
        return f"{signal.location} 存在 {check}：{signal.message}"
    return f"{signal.location} 存在以下问题：{signal.message}"


def _sample(signal: Signal) -> dict[str, Any]:
    return {"signal": signal.id, "check": signal.check, "location": signal.location, "message": signal.message,
            "role": role_of(signal), "occurredAt": format_iso(signal.occurred_at), "release": signal.release,
            "context": {key: value for key, value in signal.context.items() if key not in JUDGEMENT_KEYS}}


def samples(found: Sequence[Signal], limit: int) -> list[Signal]:
    """从最近的信号开始，按(角色, 位置)去重。"""
    chosen: list[Signal] = []
    seen: set[tuple[str | None, str]] = set()
    for signal in sorted(found, key=lambda item: (item.occurred_at, item.id), reverse=True):
        key = (role_of(signal), signal.location)
        if key not in seen:
            seen.add(key)
            chosen.append(signal)
        if len(chosen) == limit:
            break
    return chosen


def entry_points(problem: Problem, found: Sequence[Signal]) -> tuple[str, ...]:
    entries: list[str] = []
    if problem.probe in (Probe.API_FUZZ, Probe.ACCESS_LOG):
        entries.append(problem.scope.location)
    for signal in found:
        frames = [frame for frame in _frames(signal) if frame.get("file") and frame.get("line")]
        if frames:
            entries.append(f"{frames[0]['file']}:{frames[0]['line']}")
        line = signal.ctx("line")
        if problem.probe in (Probe.STATIC, Probe.INCIDENTAL) and isinstance(line, int):
            entries.append(f"{signal.location.partition(':')[0]}:{line}")
    return tuple(dict.fromkeys(entries))


def build(problem: Problem, found: Sequence[Signal], user_notes: Sequence[str] = (), sample_limit: int = 5) -> Claim:
    if not found:
        raise ValueError(f"问题 {problem.id} 没有信号，无法组装主张")
    latest = max(found, key=lambda signal: (signal.occurred_at, signal.id))
    facts = [
        Fact("出现次数", problem.occurrences),
        Fact("首次出现", f"{format_iso(problem.first_seen_at)}(版本 {problem.first_seen_release or '未知'})"),
        Fact("末次出现", f"{format_iso(problem.last_seen_at)}(版本 {problem.last_seen_release or '未知'})"),
        *(Fact(f"信号 {signal.id}", _sample(signal)) for signal in samples(found, sample_limit)),
    ]
    return Claim(statement(problem, latest), problem.title, tuple(facts), entry_points(problem, found),
                 tuple(user_notes))
