"""各来源共用的结果类型与调用骨架(44c)。

- 来源结果的状态只有 done、partial、skipped、failed；不是 done 的必须写明原因。有几个子来源的(平台错误)一个失败、
  另一个照常时为 partial，都失败才是 failed；
- state 的键是 store 的 state 表中的键(读取位置、基线、探针状态、已读记录)，值随信号在去重的同一个事务里保存；
  failed 的结果不带 state，位置不前进；
- coverage 是本次真正读到的范围(子来源名、代码文件、`方法 路由`)，去重据此判断「覆盖而没再出现」；没读到的不算；
- 各来源只抛带类型的 SourceError(平台不可用、返回内容不合格、配置缺项)，由 `guarded` 一处归类成 failed、记耗时，
  并按时限截断：一个来源卡住不拖住整轮。来源不各自吞掉、不各自重试；错误信息不含凭据。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from tightrein.collect.common.signals import Signal
from tightrein.protocol import external
from tightrein.protocol.handoff import Metrics

MILLISECONDS_PER_SECOND = 1000
PRODUCED_SIGNALS = "signals"  # metrics.produced 中的信号数：status、watch 读它


class SourceStatus(StrEnum):
    DONE = "done"
    PARTIAL = "partial"  # 有子来源失败，其余照常(失败的子来源不保存读取位置)
    SKIPPED = "skipped"
    FAILED = "failed"


# 来源的错误就是外部依赖的错误(protocol/external.py)，采集沿用 Source* 的名字
FailureKind = external.FailureKind
SourceError = external.ExternalError
SourceUnavailable = external.Unavailable
SourceInvalid = external.Invalid
SourceMisconfigured = external.Misconfigured


@dataclass
class SourceResult:
    source: str
    status: SourceStatus
    signals: list[Signal]
    read: int  # 读到的条目数(分组、日志行、请求、告警、探针、交接文档)
    window: tuple[str, str] | None
    reason: str | None  # 跳过、失败或部分失败的原因
    state: dict[str, Any]  # state 表的键 → 值；与信号同事务保存，失败时不前进
    metrics: Metrics
    coverage: list[str] = field(default_factory=list)  # 本次真正读到的范围(见模块说明)
    notes: list[str] = field(default_factory=list)  # 写进运行摘要的说明(可能漏读、读满上限、作废的探针等)

    def __post_init__(self) -> None:
        self.status = SourceStatus(self.status)
        if self.status is not SourceStatus.DONE and not self.reason:
            raise ValueError(f"{self.source}：{self.status.value} 的结果必须写明原因")
        if self.status is SourceStatus.FAILED and self.state:
            raise ValueError(f"{self.source}：failed 的结果不能带读取位置")


def skipped(source: str, reason: str) -> SourceResult:
    return SourceResult(source, SourceStatus.SKIPPED, [], 0, None, reason, {}, Metrics())


def failed(source: str, reason: str, *, notes: list[str] | None = None) -> SourceResult:
    return SourceResult(source, SourceStatus.FAILED, [], 0, None, reason, {}, Metrics(), notes=list(notes or []))


def guarded(source: str, body: Callable[[], SourceResult], *, timeout_s: float | None = None,
            monotonic: Callable[[], float] = time.monotonic) -> SourceResult:
    """调用一个来源：SourceError 归类成 failed(原因带类别)，超过 timeout_s 即按超时失败；耗时记进 metrics.duration_ms，
    信号数记进 metrics.produced.signals(所有来源统一在这里写，交接文档的 facts.read 取 SourceResult.read)。

    超时的来源在后台线程里继续跑完，但它的结果(信号与读取位置)被丢弃：位置不前进，下次从原处重读。
    其他异常(程序缺陷)原样抛给调度的错误边界。
    """
    started = monotonic()
    outcome: list[SourceResult] = []
    errors: list[BaseException] = []

    def target() -> None:
        try:
            outcome.append(body())
        except SourceError as error:
            outcome.append(failed(source, describe(error)))
        except BaseException as error:  # noqa: BLE001 - 交回调用线程再抛
            errors.append(error)

    worker = threading.Thread(target=target, name=f"collect:{source}", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        result = failed(source, f"{FailureKind.TIMEOUT.value}：超过 {timeout_s:g} 秒未完成，本次结果作废")
    elif errors:
        raise errors[0]
    else:
        result = outcome[0]
    elapsed = round((monotonic() - started) * MILLISECONDS_PER_SECOND)
    produced = {**(result.metrics.produced or {}), PRODUCED_SIGNALS: len(result.signals)}
    return replace(result, metrics=replace(result.metrics, duration_ms=elapsed, produced=produced))


def each[T](jobs: Mapping[str, Callable[[], T]], *, workers: int) -> dict[str, T | SourceError]:
    """互不依赖的子来源(两个平台、几个项目探针)并行跑、各自成败：SourceError 留在各自的结果里，不影响其他。"""
    if not jobs:
        return {}

    def attempt(job: Callable[[], T]) -> T | SourceError:
        try:
            return job()
        except SourceError as error:
            return error

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as pool:
        futures = {name: pool.submit(attempt, job) for name, job in jobs.items()}
        return {name: future.result() for name, future in futures.items()}


def describe(error: SourceError) -> str:
    return f"{error.kind.value}：{error.message}"

