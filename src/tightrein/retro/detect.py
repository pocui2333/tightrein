"""从本次运行的记录中找出 tightrein 自己的问题(retro/README.md「记什么」)：失败、浪费、误判、打扰。

数据只取本次运行留下的：
- store 的 runs：本次运行整轮失败或被中断；
- events.jsonl：本次运行碰过哪些对象(另加 issues、problems 表中本次运行开始后更新过的)；
- 这些对象目录中本次运行的 `*-handoff.json`(结论与量化数据)与 `*-started.json`(每次模型调用的最终状态与耗时)。

每一项检测各自独立：某一项出错只记进错误列表，其余照常算；某个文件读不了同样记一条，跳过它。
阈值取 controls 的 retro(settings/defaults.json)。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.protocol.handoff import Handoff, Status
from tightrein.protocol.handoff import read as read_handoff
from tightrein.protocol.naming import format_count, format_duration, kind_of, parse_duration
from tightrein.retro.records import Impact, Kind, fingerprint
from tightrein.settings.load import Settings
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.tables import runs

SECTION = "retro"
HANDOFF_GLOB = "*-handoff.json"
STARTED_GLOB = "*-started.json"
MISJUDGED_KEY = "misjudged"
# 改判(把「不成立」改判为成立)：上一次判断已记为误判时由那一条负责，不重复计数
OVERRIDE = "false_refute"
MISJUDGMENT_LABELS = {"false_confirm": "判为成立，后来证明不是", OVERRIDE: "判为不成立，用户改判为成立",
                      "false_block": "审查报的阻断项被证明是误报"}
SEVERE_CALLS = frozenset({"auth_failed", "quota_exhausted", "unavailable"})
MAJOR_CALLS = frozenset({"boundary"})
FAILED_RUNS = {"failed": "run-failed", "interrupted": "run-interrupted"}


@dataclass(frozen=True)
class Thresholds:
    step_tokens: int  # 单步(含其中全部调用)的 token
    big_tokens: int  # 达到即为「大量浪费」
    issue_tokens: int  # 单个 Issue 累计
    step_duration_ms: int
    call_duration_ms: int
    rounds: int  # 同一步来回的轮数
    lines_read: int  # 单步读取的行数(重复读代码)
    cache_read_weight: float

    @classmethod
    def from_settings(cls, settings: Settings) -> Thresholds:
        values = settings.section(SECTION)
        return cls(
            step_tokens=int(values["stepTokens"]), big_tokens=int(values["bigTokens"]),
            issue_tokens=int(values["issueTokens"]),
            step_duration_ms=int(parse_duration(values["stepDuration"]) * 1000),
            call_duration_ms=int(parse_duration(values["callDuration"]) * 1000),
            rounds=int(values["rounds"]), lines_read=int(values["linesRead"]),
            cache_read_weight=float(settings.get("resources.cacheReadWeight")),
        )


@dataclass(frozen=True)
class Finding:
    kind: Kind
    point: str  # 阶段与小步骤
    call_point: str
    phenomenon: str  # 英文短名
    fact: str  # 现象：只写事实，不含本次的数字
    impact: Impact
    subject: str | None
    detail: str  # 本次的细节与数据
    tokens: int = 0
    duration_ms: int = 0
    rounds: int = 0

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.kind, self.point, self.call_point, self.phenomenon)


@dataclass(frozen=True)
class CallMark:
    """一次模型调用的 started 标记。"""

    point: str
    subject: str | None
    status: str | None  # None：没有结束(进程被杀)
    duration_ms: int | None
    model: str | None


@dataclass
class RunData:
    run: str
    run_status: str | None
    handoffs: list[Handoff] = field(default_factory=list)
    calls: list[CallMark] = field(default_factory=list)
    issue_tokens_before: dict[str, int] = field(default_factory=dict)  # 本次运行之前该 Issue 累计的 token
    errors: list[str] = field(default_factory=list)


@dataclass
class Detection:
    findings: list[Finding]
    errors: list[str]


# 读取


def gather(layout: WorkspaceLayout, conn: sqlite3.Connection, run_id: str, *, cache_read_weight: float) -> RunData:
    """读出本次运行留下的记录。"""
    record = runs.get(conn, run_id)
    data = RunData(run_id, record.status if record is not None else None)
    subjects = _subjects(layout, conn, run_id, record, data.errors)
    for subject in sorted(subjects):
        directory = layout.subject_dir(subject)
        if not directory.is_dir():
            continue
        others = 0
        for path in sorted(directory.glob(HANDOFF_GLOB)):
            handoff = _read(path, read_handoff, data.errors)
            if handoff is None:
                continue
            if handoff.run == run_id:
                data.handoffs.append(handoff)
            else:
                others += _weighted(handoff, cache_read_weight)
        if _is_issue(subject):
            data.issue_tokens_before[subject] = others
        for path in sorted(directory.glob(STARTED_GLOB)):
            mark = _read(path, _call_mark, data.errors)
            if mark is not None and mark[0] == run_id:
                data.calls.append(mark[1])
    return data


# 检测


def detect(data: RunData, thresholds: Thresholds, *, blamed: set[str]) -> Detection:
    """blamed：已在误判记录中出现过的对象(改判时由那一条负责)。"""
    errors = list(data.errors)
    findings: list[Finding] = []
    checks: list[tuple[str, Callable[[], list[Finding]]]] = [
        ("整轮", lambda: _run_failures(data)),
        ("模型调用", lambda: _call_findings(data, thresholds)),
        ("步骤", lambda: _step_findings(data, thresholds)),
        ("Issue 用量", lambda: _issue_tokens(data, thresholds)),
        ("误判", lambda: _misjudgments(data, blamed)),
    ]
    for name, check in checks:
        try:
            findings += check()
        except Exception as error:  # noqa: BLE001 一项出错不影响其余各项(要保留的设计)
            errors.append(f"{name}检测出错：{type(error).__name__}: {error}")
    return Detection(findings, errors)


# 各项检测


def _run_failures(data: RunData) -> list[Finding]:
    phenomenon = FAILED_RUNS.get(data.run_status or "")
    if phenomenon is None:
        return []
    stage = data.run.rsplit("-", 1)[-1]
    fact = "整轮运行失败" if data.run_status == "failed" else "整轮运行被中断"
    return [Finding(Kind.FAILURE, stage, stage, phenomenon, fact, Impact.SEVERE, None, f"运行 {data.run}：{fact}")]


def _call_findings(data: RunData, thresholds: Thresholds) -> list[Finding]:
    found = []
    for call in data.calls:
        step = _step_of(call.point)
        where = f"{_subject_text(call.subject)}的 {call.point}({call.model or '未知模型'})"
        if call.status is None:
            found.append(Finding(Kind.FAILURE, step, call.point, "call-unfinished", "模型调用没有结束(进程被终止)",
                                 Impact.MAJOR, call.subject, f"{where}没有结束"))
        elif call.status != "ok":
            impact = (Impact.SEVERE if call.status in SEVERE_CALLS
                      else Impact.MAJOR if call.status in MAJOR_CALLS else Impact.MINOR)
            found.append(Finding(Kind.FAILURE, step, call.point, f"call-{call.status.replace('_', '-')}",
                                 f"模型调用以 {call.status} 结束", impact, call.subject,
                                 f"{where}以 {call.status} 结束，耗时 {_duration(call.duration_ms)}",
                                 duration_ms=call.duration_ms or 0))
        if call.duration_ms is not None and call.duration_ms > thresholds.call_duration_ms:
            found.append(Finding(Kind.WASTE, step, call.point, "call-slow", "单次模型调用耗时超过阈值", Impact.MINOR,
                                 call.subject, f"{where}耗时 {_duration(call.duration_ms)}",
                                 duration_ms=call.duration_ms - thresholds.call_duration_ms))
    return found


def _step_findings(data: RunData, thresholds: Thresholds) -> list[Finding]:
    found = []
    for handoff in data.handoffs:
        point, subject, metrics = handoff.point, handoff.subject, handoff.metrics
        where = f"{_subject_text(subject)}的 {point}" + (f" 第 {handoff.round} 轮" if handoff.round else "")
        if handoff.status is Status.FAILED:
            found.append(Finding(Kind.FAILURE, point, point, "step-failed", "这一步失败停下，需要用户处理",
                                 Impact.SEVERE, subject, f"{where}失败：{handoff.summary}"))
        elif handoff.status is Status.PENDING:
            found.append(Finding(Kind.INTERRUPTION, point, point, "waiting-user", "停在人工关卡，等用户决定",
                                 Impact.TRIVIAL, subject, f"{where}等用户决定：{handoff.summary}"))
        tokens = _weighted(handoff, thresholds.cache_read_weight)
        if tokens > thresholds.step_tokens:
            impact = Impact.MAJOR if tokens >= thresholds.big_tokens else Impact.MINOR
            found.append(Finding(Kind.WASTE, point, point, "step-tokens", "单步 token 超过阈值", impact, subject,
                                 f"{where}共 {metrics.calls or 0} 次调用、{format_count(tokens)} token",
                                 tokens=tokens - thresholds.step_tokens))
        if metrics.duration_ms is not None and metrics.duration_ms > thresholds.step_duration_ms:
            found.append(Finding(Kind.WASTE, point, point, "step-slow", "单步耗时超过阈值", Impact.MINOR, subject,
                                 f"{where}耗时 {_duration(metrics.duration_ms)}",
                                 duration_ms=metrics.duration_ms - thresholds.step_duration_ms))
        if metrics.rounds is not None and metrics.rounds > thresholds.rounds:
            found.append(Finding(Kind.WASTE, point, point, "many-rounds", "同一步来回多轮", Impact.MINOR, subject,
                                 f"{where}来回 {metrics.rounds} 轮", rounds=metrics.rounds - thresholds.rounds))
        if metrics.lines_read is not None and metrics.lines_read > thresholds.lines_read:
            found.append(Finding(Kind.WASTE, point, point, "read-much", "单步读代码的行数超过阈值", Impact.MINOR,
                                 subject, f"{where}读了 {metrics.files_read or 0} 个文件、{metrics.lines_read} 行"))
    return found


def _issue_tokens(data: RunData, thresholds: Thresholds) -> list[Finding]:
    """单个 Issue 累计 token 在本次运行中越过阈值时记一次(之前已越过的不再重复记)。"""
    found = []
    for subject, before in data.issue_tokens_before.items():
        total = before
        for handoff in sorted((item for item in data.handoffs if item.subject == subject),
                              key=lambda item: item.created_at or ""):
            previous, total = total, total + _weighted(handoff, thresholds.cache_read_weight)
            if previous < thresholds.issue_tokens <= total:
                stage = handoff.point.split(".")[0]
                impact = Impact.MAJOR if total - previous >= thresholds.big_tokens else Impact.MINOR
                found.append(Finding(Kind.WASTE, stage, stage, "issue-tokens", "单个 Issue 累计 token 超过阈值",
                                     impact, subject,
                                     f"Issue {subject} 在 {handoff.point} 累计到 {format_count(total)} token",
                                     tokens=total - thresholds.issue_tokens))
    return found


def _misjudgments(data: RunData, blamed: set[str]) -> list[Finding]:
    """评估与审查在交接的必填事实 misjudged 中写明的误判：{kind, point, detail}。"""
    found = []
    counted = set(blamed)
    for handoff in data.handoffs:
        mark = handoff.facts.get(MISJUDGED_KEY)
        if not isinstance(mark, dict):
            continue
        kind = str(mark.get("kind"))
        subject = handoff.subject
        if kind == OVERRIDE and subject in counted:
            continue
        counted.add(subject)
        point = str(mark.get("point") or handoff.point)
        fact = MISJUDGMENT_LABELS.get(kind, kind)
        found.append(Finding(Kind.MISJUDGMENT, _step_of(point), point, kind.replace("_", "-"), fact, Impact.MAJOR,
                             subject, f"{_subject_text(subject)}：{mark.get('detail') or fact}"))
    return found


# 内部


def _subjects(layout: WorkspaceLayout, conn: sqlite3.Connection, run_id: str, record: runs.Run | None,
              errors: list[str]) -> set[str]:
    found = {run_id}
    events = layout.events(run_id)
    if events.is_file():
        for number, line in enumerate(events.read_text(encoding="utf-8").splitlines(), start=1):
            try:
                subject = json.loads(line).get("subject")
            except (json.JSONDecodeError, AttributeError) as error:
                errors.append(f"{events}:{number} 读不懂：{error}")
                continue
            if subject:
                found.add(str(subject))
    if record is not None:
        since = record.started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        for table in ("issues", "problems"):
            found |= {row[0] for row in conn.execute(f"SELECT id FROM {table} WHERE updated_at >= ?", (since,))}
    return found


def _read(path: Path, reader: Callable[[Path], Any], errors: list[str]) -> Any:
    try:
        return reader(path)
    except (OSError, ValueError, KeyError, TypeError) as error:
        errors.append(f"{path} 读不了：{type(error).__name__}: {error}")
        return None


def _call_mark(path: Path) -> tuple[str, CallMark]:
    data = json.loads(path.read_text(encoding="utf-8"))
    ended = data.get("endedAt") is not None
    return data["run"], CallMark(data["point"], data.get("subject"), data.get("status") if ended else None,
                                 data.get("durationMs"), data.get("model"))


def _weighted(handoff: Handoff, cache_read_weight: float) -> int:
    tokens = handoff.metrics.tokens
    return tokens.weighted(cache_read_weight) if tokens is not None else 0


def _step_of(point: str) -> str:
    """调用点所在的阶段与小步骤：取前两段(`implement.review.deep` → `implement.review`)。"""
    return ".".join(point.split(".")[:2])


def _is_issue(subject: str) -> bool:
    try:
        return kind_of(subject) == "issue"
    except ValueError:
        return False


def _subject_text(subject: str | None) -> str:
    if subject is None:
        return "本次运行"
    try:
        kind = kind_of(subject)
    except ValueError:
        return subject
    return {"issue": f"Issue {subject} ", "problem": f"问题 {subject} ", "run": f"运行 {subject} "}[kind]


def _duration(milliseconds: int | None) -> str:
    return "未知" if milliseconds is None else format_duration(milliseconds / 1000)

