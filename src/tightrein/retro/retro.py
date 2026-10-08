"""每次运行结束时的复盘(retro/README.md)：检测 → 按指纹合并或新建 → 评级 → 新记录写解决思路。

- 同一次运行中指纹相同的发现合成一次出现(细节逐条保留)；
- 已有记录：追加这次出现、更新最近发现时间与次数、重新评级；不处理的只追加不再提醒，已处理的重新列为待看；
- 新记录：编号取已有最大编号加一，调用一次模型写解决思路；
- 不自动写知识库、不出周报；落盘一份 `51-retro.detect-handoff.json`(新建与追加的记录、各自评级)。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tightrein.agents.call import call
from tightrein.protocol.handoff import Handoff, Metrics, Status, Tokens, check_facts, load_schema
from tightrein.protocol.handoff import write as write_handoff
from tightrein.protocol.naming import FileName, format_iso
from tightrein.retro import records
from tightrein.retro.detect import Detection, Finding, Thresholds, detect, gather
from tightrein.retro.idea import Invoke, write_idea
from tightrein.retro.rating import RatingRule, rate
from tightrein.retro.records import Kind, Occurrence, Record, RecordStatus

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

POINT = "retro.detect"
SCHEMA_PATH = Path(__file__).with_name("retro.schema.json")


@dataclass
class RetroOutcome:
    created: list[str]
    appended: list[str]
    ratings: dict[str, str]  # 记录编号 → 评级
    errors: list[str]
    status: Status
    metrics: Metrics


def retro(runtime: Runtime, *, invoke: Invoke = call) -> RetroOutcome:
    started = time.monotonic()
    layout = runtime.workspace
    thresholds = Thresholds.from_settings(runtime.settings)
    rule = RatingRule.from_settings(runtime.settings)
    existing = records.load_all(layout)
    by_fingerprint = {record.fingerprint: record for record in existing}
    blamed = {subject for record in existing if record.kind is Kind.MISJUDGMENT for subject in record.subjects}
    data = gather(layout, runtime.conn, runtime.run, cache_read_weight=thresholds.cache_read_weight)
    detection = detect(data, thresholds, blamed=blamed)
    now = format_iso(runtime.clock.now())
    errors = list(detection.errors)
    created: list[Record] = []
    appended: list[Record] = []
    tokens, calls = Tokens(), 0
    for fingerprint, findings in _grouped(detection).items():
        occurrence = _occurrence(runtime.run, now, findings)
        record = by_fingerprint.get(fingerprint)
        if record is not None:
            _append(record, occurrence, rule)
            appended.append(records.save(layout, record))
            continue
        record = _new(records.next_id([*existing, *created]), findings[0], occurrence, now, rule)
        idea = write_idea(runtime, record, invoke=invoke)
        calls += 1
        tokens.add(idea.result.tokens)
        if idea.idea is None:
            errors.append(f"{record.id} 的解决思路没有写成：{idea.result.status.value} {idea.result.error or ''}".strip())
        record.idea, record.settle = idea.idea, idea.settle
        created.append(records.save(layout, record))
    metrics = Metrics(duration_ms=int((time.monotonic() - started) * 1000), calls=calls, retries=None, rounds=None,
                      tokens=tokens, produced={"created": len(created), "appended": len(appended)})
    outcome = RetroOutcome(
        created=[record.id for record in created], appended=[record.id for record in appended],
        ratings={record.id: record.rating for record in (*created, *appended)}, errors=errors,
        status=Status.PASSED, metrics=metrics,
    )
    _hand_off(runtime, created, appended, outcome)
    return outcome


# 内部


def _grouped(detection: Detection) -> dict[str, list[Finding]]:
    groups: dict[str, list[Finding]] = {}
    for finding in detection.findings:
        groups.setdefault(finding.fingerprint, []).append(finding)
    return groups


def _occurrence(run: str, now: str, findings: list[Finding]) -> Occurrence:
    subjects = tuple(dict.fromkeys(item.subject for item in findings if item.subject is not None))
    return Occurrence(run=run, at=now, count=len(findings), subjects=subjects,
                      details=tuple(item.detail for item in findings), impact=max(item.impact for item in findings),
                      tokens=sum(item.tokens for item in findings),
                      duration_ms=sum(item.duration_ms for item in findings),
                      rounds=sum(item.rounds for item in findings))


def _append(record: Record, occurrence: Occurrence, rule: RatingRule) -> None:
    # 同一次运行重复复盘(续跑)时替换这次的出现，不重复计数
    record.occurrences = [item for item in record.occurrences if item.run != occurrence.run] + [occurrence]
    record.last_seen = occurrence.at
    record.rating = rate(record, rule)
    if record.status is RecordStatus.DONE:
        record.status = RecordStatus.OPEN


def _new(number: str, finding: Finding, occurrence: Occurrence, now: str, rule: RatingRule) -> Record:
    record = Record(id=number, rating="P3", status=RecordStatus.OPEN, kind=finding.kind, point=finding.point,
                    call_point=records.call_point_group(finding.call_point), phenomenon=finding.phenomenon,
                    fact=finding.fact, first_seen=now, last_seen=now, occurrences=[occurrence])
    record.rating = rate(record, rule)
    return record


def _hand_off(runtime: Runtime, created: list[Record], appended: list[Record], outcome: RetroOutcome) -> None:
    facts = {
        "created": [_fact(record) for record in created],
        "appended": [_fact(record) for record in appended],
        "errors": outcome.errors,
    }
    check_facts(POINT, facts, load_schema(SCHEMA_PATH))
    summary = f"新建 {len(created)} 条、追加 {len(appended)} 条复盘记录" + (
        f"，{len(outcome.errors)} 项出错" if outcome.errors else "")
    handoff = Handoff(point=POINT, subject=runtime.run, run=runtime.run, status=outcome.status, summary=summary,
                      facts=facts, metrics=outcome.metrics, created_at=format_iso(runtime.clock.now()))
    write_handoff(runtime.workspace.step_file(runtime.run, FileName(POINT, "handoff", "json")), handoff)
    runtime.events.emit(run=runtime.run, subject=None, point=POINT, kind="effect", summary=summary)


def _fact(record: Record) -> dict[str, object]:
    return {"id": record.id, "rating": record.rating, "kind": record.kind.value, "point": record.point,
            "count": record.count}
