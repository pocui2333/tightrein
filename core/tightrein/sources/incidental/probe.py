"""IncidentalProbe(architecture/04 6.2 到 6.4)。

日常读取尚未读过或内容已变化的分诊与修复交接文档；给出 import_archive 时另读取迁移归档目录中的 markdown 报告
(路径与哈希都已读过的跳过)。每个成功读取的来源生成一条已读记录(路径、内容哈希、读取时间、产出的信号数)，随结果
返回，由 collect 在写入信号的同一事务中保存；读取或解析失败的来源不记录，下次重试，原因写入 notes，状态为 partial。
提取不到文件的发现不产出信号，计入 stats.unlocated 并列入 notes，由用户手动补登。没有任何新来源时为 skipped。
coverage 为空：任务外发现不作为任何问题的覆盖运行。没有档位，传入的档位被忽略。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.enums import ProbeLevel, RunStatus
from tightrein.sources.base import PROBE_LEVELS, ProbeOptions, ProbeOutcome, ProbeTarget, skipped
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.sources.common.signals import RandomBytes, SignalFactory
from tightrein.sources.incidental import archive_import, handoff_source, mapping
from tightrein.sources.incidental.handoff_source import SourceRead
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import incidental_sources
from tightrein.store.repos.incidental_sources import IncidentalSource

NOTHING_NEW = "没有新的或变化的任务外发现来源"


@dataclass(frozen=True)
class IncidentalDependencies:
    conn: sqlite3.Connection
    layout: WorkspaceLayout
    redactor: ProbeRedactor
    randomness: RandomBytes = os.urandom


class IncidentalProbe:
    name = ProbeKind.INCIDENTAL
    levels = PROBE_LEVELS[ProbeKind.INCIDENTAL]

    def __init__(self, dependencies: IncidentalDependencies) -> None:
        self.deps = dependencies

    def _archive(self, options: ProbeOptions) -> list[SourceRead]:
        if options.import_archive is None:
            return []
        reads = []
        for read in archive_import.scan(options.import_archive):
            known = incidental_sources.get(self.deps.conn, read.path)
            if read.error is None and known is not None and known.content_hash == read.content_hash:
                continue
            reads.append(read)
        return reads

    def run(self, target: ProbeTarget, level: ProbeLevel | None, options: ProbeOptions) -> ProbeOutcome:
        reads = [*handoff_source.unread(self.deps.conn, self.deps.layout.root), *self._archive(options)]
        if not reads:
            return skipped(NOTHING_NEW)
        factory = SignalFactory(target, self.name, self.deps.redactor, randomness=self.deps.randomness)
        now = target.clock.now()
        signals = []
        sources = []
        notes = []
        unlocated = []
        findings = 0
        for read in reads:
            if read.error is not None:
                notes.append(f"来源读取失败，下次重试：{read.error}")
                continue
            produced = mapping.to_signals(read.findings, factory)
            signals += produced
            findings += len(read.findings)
            unlocated += [finding.text for finding in read.findings if finding.location is None]
            sources.append(IncidentalSource(read.path, read.content_hash, now, len(produced)))
        for text in unlocated:
            notes.append(f"提取不到文件，需要手动补登：{text[:self.deps.redactor.limits.unlocated_excerpt_chars]}")
        stats = {"sources": len(sources), "findings": findings, "signals": len(signals), "unlocated": len(unlocated)}
        status = RunStatus.PARTIAL if len(sources) < len(reads) else RunStatus.OK
        return ProbeOutcome(status, tuple(signals), stats=stats, notes=tuple(notes), sources=tuple(sources))
