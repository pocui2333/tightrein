"""从迁移归档的 markdown 报告一次性导入任务外发现(architecture/04 6.2，design 9.10)。

只在 `collect --probe incidental --import-archive <目录>` 时执行：递归扫描目录下的 .md 文件，找到标题含「任务外发现」
的一节(到下一个同级或更高级的标题为止)，其中每个顶层列表项(`-`、`*`、`+` 或 `1.`，缩进小于 2 个空格)作为一条
发现，缩进的续行与嵌套列表并入该项。报告日期依次取文件名中的日期、正文中第一个日期、文件的修改时间。
来源路径登记为文件的绝对路径；位置由 locate 从原文中提取。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from datetime import date, datetime, timezone
from pathlib import Path

from tightrein.sources.incidental.handoff_source import SourceRead
from tightrein.sources.incidental.locate import locate
from tightrein.sources.incidental.mapping import Finding

SECTION_TITLE = "任务外发现"
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LIST_ITEM = re.compile(r"^( {0,1})(?:[-*+]|\d+[.)])\s+(.*)$")
DATE = re.compile(r"(\d{4})-?(\d{2})-?(\d{2})")
ARCHIVE_STAGE = "archive"


def _date(text: str) -> date | None:
    match = DATE.search(text)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def report_date(path: Path, text: str) -> date:
    return _date(path.name) or _date(text) or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date()


def items(text: str) -> Iterator[str]:
    """文中各「任务外发现」一节的顶层列表项。"""
    level: int | None = None
    current: list[str] = []
    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading is not None:
            if current:
                yield " ".join(current)
                current = []
            depth = len(heading.group(1))
            if SECTION_TITLE in heading.group(2):
                level = depth
            elif level is not None and depth <= level:
                level = None
            continue
        if level is None:
            continue
        item = LIST_ITEM.match(line)
        if item is not None:
            if current:
                yield " ".join(current)
            current = [item.group(2).strip()]
        elif line.strip() and current:
            current.append(re.sub(r"^\s*(?:[-*+]|\d+[.)])?\s*", "", line).strip())
        elif not line.strip() and current:
            yield " ".join(current)
            current = []
    if current:
        yield " ".join(current)


def read(path: Path) -> SourceRead:
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return SourceRead(str(path), "", error=f"{path} 无法读取：{type(error).__name__}")
    day = report_date(path, text)
    occurred = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    findings = tuple(Finding(item, locate(item), str(path), ARCHIVE_STAGE, occurred) for item in items(text))
    return SourceRead(str(path), hashlib.sha256(data).hexdigest(), findings)


def scan(directory: Path) -> list[SourceRead]:
    if not directory.is_dir():
        return [SourceRead(str(directory), "", error=f"归档目录不存在：{directory}")]
    return [read(path) for path in sorted(directory.rglob("*.md")) if path.is_file()]
