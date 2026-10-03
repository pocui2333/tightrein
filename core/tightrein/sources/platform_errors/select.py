"""按级别筛选日志平台取回的条目，截取本项目帧。

只保留 level 属于 sources.platform-errors.levels(缺省 error、critical)的条目；每条的 frames 中 isProject 为真的帧
按原顺序取前 10 个。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tightrein.domain.enums import LogLevel


@dataclass(frozen=True)
class SelectedEntry:
    entry: Mapping[str, Any]
    project_frames: tuple[Mapping[str, Any], ...]


def project_frames(entry: Mapping[str, Any], limit: int) -> tuple[Mapping[str, Any], ...]:
    return tuple(frame for frame in entry.get("frames") or () if frame.get("isProject"))[:limit]


def apply(entries: Iterable[Mapping[str, Any]], levels: Sequence[LogLevel], frames: int) -> list[SelectedEntry]:
    """frames 为保留的本项目帧数(runtime.sources.projectFrames)。"""
    wanted = {level.value for level in levels}
    return [SelectedEntry(entry, project_frames(entry, frames)) for entry in entries if entry.get("level") in wanted]
