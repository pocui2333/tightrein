"""取证调度(collect.static.verify)：每条主张单独一次只读调用，判定成立、有条件成立、不成立或证据不足。

- 取证只给主张、位置与程序截取的那段代码，不给审查过程(不让审查的推理先入为主)；
- 两档：先由便宜的模型初判(collect.static.verify)，拿不准的才交给强模型(collect.static.verify.deep)。升级的条件：
  初判没完成(额度用尽与越界除外，照原样处理)、判证据不足、判不成立但把握不高(便宜模型只在有把握时才能单独否掉
  主张，宁可多花一次也不漏掉真问题)、主张的严重度在 escalateSeverities 中(缺省为高)。第二档只多拿初判记下的代码
  事实作线索，看不到初判的判定；两档的调用都计入用量；
- 不同文件的取证并行(并发数取 resources.concurrency.modelCalls，全局模型调用另受 Slots 限制)，同一文件的按顺序；
- 取证在线程池的线程里调用模型：数据库连接(连同熔断、额度、用量计数)在那个线程里另开(protocol/runtime.isolated，
  与采集并行跑来源同一做法)，不用别的线程开的连接；
- 预算用尽(额度或用量到限)后不再发起新的取证，没取证的留到下次；
- 环境级越界(只读 worktree 被改等)：停下，整次巡检作废；只作废那一次的越界与其他失败只丢那一条的产出。
同一文件的主张合并在一次调用中取证(第二批)尚未做。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from tightrein.agents.call import call
from tightrein.agents.result import CallResult, CallStatus
from tightrein.collect.static import context, review
from tightrein.collect.static.claims import Claim
from tightrein.collect.static.review import EXHAUSTED, VERIFY, VERIFY_DEEP, Caller
from tightrein.protocol.runtime import Runtime, isolated

CONFIRMED = "confirmed"
CONDITIONAL = "conditional"
REFUTED = "refuted"
INSUFFICIENT = "insufficient"
SIGNAL_VERDICTS = (CONFIRMED, CONDITIONAL)
HIGH_CONFIDENCE = "high"


@dataclass(frozen=True)
class Verification:
    claim: Claim
    result: CallResult  # 采用的那一档的结果
    completed_at: datetime
    first: CallResult | None = None  # 升级到第二档时，第一档的结果(只计入用量)
    escalated: str | None = None  # 升级的原因

    @property
    def calls(self) -> list[CallResult]:
        return [*([self.first] if self.first is not None else []), self.result]

    @property
    def output(self) -> dict[str, Any] | None:
        return self.result.output if self.result.ok else None

    @property
    def verdict(self) -> str | None:
        return None if self.output is None else str(self.output["verdict"])


@dataclass
class VerifyRun:
    done: list[Verification] = field(default_factory=list)
    not_started: list[Claim] = field(default_factory=list)  # 预算用尽或作废后没取证的：留到下次
    calls: list[CallResult] = field(default_factory=list)  # 全部取证调用(含失败的与升级前的初判)：计入用量
    escalated: int = 0  # 交给第二档的条数
    notes: list[str] = field(default_factory=list)
    violated: bool = False
    degraded: bool = False


VerifyOne = Callable[[Claim, int], Verification]
Isolate = Callable[[Runtime], AbstractContextManager[Runtime]]


def run(claims: Sequence[Claim], verify_one: VerifyOne, *, workers: int) -> VerifyRun:
    """按文件分组并行；编号从 1 起，用作调用文件名中的轮次，同一次运行内互不覆盖。"""
    groups: dict[str, list[tuple[int, Claim]]] = {}
    for number, claim in enumerate(claims, start=1):
        groups.setdefault(claim.file, []).append((number, claim))
    result = VerifyRun()
    stop = threading.Event()
    lock = threading.Lock()

    def one_file(items: list[tuple[int, Claim]]) -> None:
        for number, claim in items:
            if stop.is_set():
                with lock:
                    result.not_started.append(claim)
                continue
            verification = verify_one(claim, number)
            with lock:
                _record(result, verification, stop)

    if groups:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(groups)))) as pool:
            for future in [pool.submit(one_file, items) for items in groups.values()]:
                future.result()
    return result


def escalation(claim: Claim, result: CallResult, severities: frozenset[str]) -> str | None:
    """初判要不要交给第二档：返回原因，不用时为 None。额度用尽与越界不升级，照原样处理。"""
    if result.status in EXHAUSTED or result.status is CallStatus.BOUNDARY:
        return None
    if not result.ok or result.output is None:
        return f"初判没有完成({result.status.value})"
    verdict = result.output.get("verdict")
    if verdict == INSUFFICIENT:
        return "初判证据不足"
    if verdict == REFUTED and result.output.get("confidence") != HIGH_CONFIDENCE:
        return "初判不成立但把握不高"
    if claim.severity in severities:
        return f"主张严重度为 {claim.severity}"
    return None


def leads(result: CallResult) -> str:
    """交给第二档的线索：初判记下的代码事实(位置与那里做了什么)，不含判定与分析。"""
    facts = (result.output or {}).get("facts") or []
    if not facts:
        return review.NONE
    return "\n".join(f"- `{fact['location']}`：{fact['observation']}" for fact in facts)


def verifier(runtime: Runtime, *, workdir: Path, caller: Caller = call,
             isolate: Isolate = isolated) -> VerifyOne:
    """一条主张的取证：提示中给主张、入口与程序截取的代码；在调用它的线程里另开连接(isolate)。"""
    limits = context.Limits.from_section(runtime.settings.section("collect.static"))
    severities = frozenset(runtime.settings.control(VERIFY, "escalateSeverities"))

    def verify_one(claim: Claim, number: int) -> Verification:
        variables = {
            "claim": (f"`{claim.file}:{claim.line}` 存在以下问题({claim.rule_or_pattern}，严重度 {claim.severity})："
                      f"{claim.statement}"),
            "trigger": claim.trigger,
            "entry": f"{claim.file}:{claim.line}",
            "code": context.excerpt(workdir, claim.file, claim.line, limits),
        }
        with isolate(runtime) as local:
            first = review.call_point(local, VERIFY, variables, workdir=workdir, round=number, caller=caller)
            reason = escalation(claim, first, severities)
            if reason is None:
                return Verification(claim, first, runtime.clock.now())
            deep = review.call_point(local, VERIFY_DEEP, {**variables, "leads": leads(first)}, workdir=workdir,
                                     round=number, caller=caller)
        return Verification(claim, deep, runtime.clock.now(), first=first, escalated=reason)

    return verify_one


def _record(result: VerifyRun, verification: Verification, stop: threading.Event) -> None:
    where = f"{verification.claim.file}:{verification.claim.line}"
    call_result = verification.result
    result.calls += verification.calls
    result.escalated += verification.escalated is not None
    if call_result.ok:
        result.done.append(verification)
        return
    if review.violated_environment(call_result):
        result.violated = True
        stop.set()
        return
    result.degraded = True
    if call_result.status in EXHAUSTED:
        if not stop.is_set():
            result.notes.append(f"取证到了额度或用量上限({call_result.status.value})，其余主张留到下次")
        result.not_started.append(verification.claim)
        stop.set()
        return
    result.notes.append(f"{where} 的取证未完成({call_result.status.value})：{call_result.error or '没有说明'}")
