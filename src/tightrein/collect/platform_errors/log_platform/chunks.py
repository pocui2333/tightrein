"""日志平台方法交出的东西：按流(标签组合)分开的原文片段，交给日志解析方法(log_parse/)或访问日志的解析。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Chunk:
    stream: str  # 流名：排序后的 {k="v",...}
    text: str  # 按时间排列的原文行，每行以换行结尾
    start_position: int  # 片段起点的字节位置
    modified_at: datetime | None  # 片段中最晚一条的时间

    @property
    def end_position(self) -> int:
        return self.start_position + len(self.text.encode("utf-8"))


@dataclass(frozen=True)
class LogRead:
    chunks: list[Chunk]
    truncated: bool  # 读满条数上限，窗口中其余的下次接着读
    oldest_available: datetime | None  # 平台还能查到的最早时间(按保留天数估算)

    def last_time(self) -> datetime | None:
        """已读片段中最晚一条的时间：读满上限时读取位置停在这里。"""
        found = [chunk.modified_at for chunk in self.chunks if chunk.modified_at is not None]
        return max(found) if found else None

    def lines(self) -> list[str]:
        return [line for chunk in self.chunks for line in chunk.text.splitlines()]
