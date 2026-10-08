"""任务外发现的采集流程：找未读过或内容已变的交接文档 → 取出发现 → 转成信号 → 记录已读。不调用模型、不读代码。

- 已读记录(state 表 `collect.incidental:read`，路径 → 内容哈希)随信号在去重的同一个事务里保存；只保留仍存在的
  交接文档，已清理的不再记着；
- 单个交接文档读不了或解析失败时跳过它、不记已读，下次重试，状态为 partial；全都失败为 failed；没有新来源时为 skipped；
- 覆盖范围永远为空：任务外发现不会因为「覆盖运行里没出现」被判为已解决。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tightrein.collect.common.signals import Signal, factory_for
from tightrein.collect.common.source import SourceResult, SourceStatus, failed, skipped
from tightrein.collect.incidental import handoffs, mapping
from tightrein.protocol.handoff import Metrics
from tightrein.store.tables import state

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

SOURCE = "collect.incidental"
READ_KEY = f"{SOURCE}:read"
NOTHING_NEW = "没有新的或内容变了的交接文档"


def collect(runtime: Runtime) -> SourceResult:
    module = runtime.setup.module(SOURCE)
    if not runtime.setup.enabled(SOURCE):
        return skipped(SOURCE, f"未启用：{module.reason or '接入清单中为 disabled'}")
    points: dict[str, str] = runtime.settings.section(SOURCE)["points"]
    known: dict[str, str] = dict(state.get(runtime.conn, READ_KEY) or {})
    found = list(handoffs.unread(runtime.workspace, points, known, runtime.clock.now()))
    reads = [item for _, item in found if item is not None]
    if not reads:
        return skipped(SOURCE, NOTHING_NEW)
    factory = factory_for(runtime, SOURCE)
    signals: list[Signal] = []
    errors: list[str] = []
    dropped = findings = 0
    current = {path: known[path] for path, item in found if item is None}
    for read in reads:
        if read.error is not None:
            errors.append(f"跳过，下次重试：{read.error}")
            continue
        produced, skipped_count = mapping.to_signals(read.findings, factory)
        signals += produced
        dropped += skipped_count
        findings += len(read.findings)
        current[read.path] = read.content_hash
    if len(errors) == len(reads):
        return failed(SOURCE, "；".join(errors))
    notes = [f"{dropped} 条发现的类别不在缺陷、安全、性能、数据之内，已丢弃"] if dropped else []
    produced_counts = {"sources": len(reads) - len(errors), "findings": findings, "dropped": dropped,
                       "signals": len(signals)}
    return SourceResult(SOURCE, SourceStatus.PARTIAL if errors else SourceStatus.DONE, signals, len(reads), None,
                        "；".join(errors) or None, {READ_KEY: current}, Metrics(produced=produced_counts),
                        notes=notes)
