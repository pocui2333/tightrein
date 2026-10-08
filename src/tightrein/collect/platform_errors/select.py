"""日志条目按级别筛选，并截取本项目帧。

只保留级别在配置中的条目(缺省 error、critical)；每条的帧中属于本项目的按原顺序取前 projectFrames 个。
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass

from tightrein.collect.platform_errors.log_parse.entries import Frame, LogEntry


@dataclass(frozen=True)
class SelectedEntry:
    entry: LogEntry
    project_frames: tuple[Frame, ...]


def apply(entries: Iterable[LogEntry], levels: Collection[str], frames: int) -> list[SelectedEntry]:
    wanted = frozenset(levels)
    return [SelectedEntry(entry, project_frames(entry, frames)) for entry in entries if entry.level in wanted]


def project_frames(entry: LogEntry, limit: int) -> tuple[Frame, ...]:
    return tuple(frame for frame in entry.frames if frame.is_project)[:limit]
