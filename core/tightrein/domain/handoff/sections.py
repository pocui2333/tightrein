"""Markdown 正文的小节操作，交接文档与 Issue 文件共用。

小节按行首 `#` 的级别切分(二级为基础小节，三级为「内容」下的小节)，不识别代码块中的标题。标题到键的映射(aliases)
由调用方给出，于是任何语言的标题与旧标题都能识别为同一个键。数据块是信息串为 `yaml data:<标签>` 的代码块。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime, tzinfo

from tightrein.domain.handoff import types

BLOCK = re.compile(r"^```yaml data:([A-Za-z][\w-]*)[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
FRONTMATTER = "---"
FENCE = re.compile(r"^\s*(```|~~~)")


def _heading(level: int) -> re.Pattern[str]:
    return re.compile(rf"^{'#' * level} +(.+?)\s*$", re.MULTILINE)


def _fenced(body: str) -> list[tuple[int, int]]:
    """代码块(``` 或 ~~~ 围起的行)在正文中的起止位置；没有闭合的代码块延续到正文末尾。"""
    spans, start, offset = [], None, 0
    for line in body.splitlines(keepends=True):
        if FENCE.match(line):
            if start is None:
                start = offset
            else:
                spans.append((start, offset + len(line)))
                start = None
        offset += len(line)
    if start is not None:
        spans.append((start, len(body)))
    return spans


def split(body: str, level: int = 2) -> dict[str, str]:
    """正文中这一级的各标题与其内容(到下一个同级标题为止)，按出现顺序；代码块中的标题行不算。"""
    fenced = _fenced(body)
    found = [match for match in _heading(level).finditer(body)
             if not any(start <= match.start() < end for start, end in fenced)]
    return {match.group(1): body[match.end():found[index + 1].start() if index + 1 < len(found) else len(body)]
            for index, match in enumerate(found)}


def key_of(title: str, aliases: Mapping[str, str]) -> str | None:
    return aliases.get(title.strip())


def find(sections: Mapping[str, str], key: str, aliases: Mapping[str, str]) -> str | None:
    """按键取一节的内容；没有时为 None。同一键有多节时依次拼接。"""
    parts = [text.strip() for title, text in sections.items() if key_of(title, aliases) == key]
    return "\n\n".join(parts) if parts else None


def title_in(body: str, key: str, aliases: Mapping[str, str], level: int = 2) -> str | None:
    """正文中这一节实际使用的标题。"""
    return next((title for title in split(body, level) if key_of(title, aliases) == key), None)


def keys_in(body: str, aliases: Mapping[str, str], level: int = 2) -> list[str]:
    """正文中出现的小节键，按出现顺序。"""
    return [key for key in (key_of(title, aliases) for title in split(body, level)) if key is not None]


def language_of(body: str, headings: Mapping[str, Mapping[str, str]], default: str) -> str:
    """正文已有小节标题所用的语言；认不出时为 default。"""
    for title in split(body):
        for titles in headings.values():
            found = next((code for code, text in titles.items() if text == title), None)
            if found is not None:
                return found
    return default


def append_line(body: str, title: str, line: str) -> str:
    """在二级标题为 title 的小节末尾追加一行；没有这一节时在文末新建。"""
    heading = f"## {title}"
    lines = body.rstrip("\n").split("\n")
    if heading not in lines:
        return body.rstrip("\n") + f"\n\n{heading}\n\n{line}\n"
    start = lines.index(heading)
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return "\n".join([*lines[:end], line, *lines[end:]]) + "\n"


def local_time(at: datetime, zone: tzinfo | None = None) -> str:
    local = at.astimezone() if zone is None else at.astimezone(zone)
    return f"{local.strftime('%Y-%m-%d %H:%M')}({local.tzname()})"


def history_line(at: datetime, text: str, zone: tzinfo | None = None) -> str:
    """「历史」中的一行：绝对日期、本地时间与时区。"""
    return f"- {local_time(at, zone)} {text}"


def blocks(body: str) -> dict[str, str]:
    """数据块的标签到原文；同一标签出现多次时取第一个。"""
    found: dict[str, str] = {}
    for match in BLOCK.finditer(body):
        found.setdefault(match.group(1), match.group(2))
    return found


def without_blocks(text: str) -> str:
    return BLOCK.sub("", text)


def strip_frontmatter(text: str) -> str:
    """去掉开头的头信息，只留正文；没有头信息时原样返回。"""
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != FRONTMATTER:
        return text
    end = next((index for index in range(1, len(lines)) if lines[index].rstrip("\r") == FRONTMATTER), None)
    return text if end is None else "\n".join(lines[end + 1:])


def extract(text: str, keys: Iterable[str], kind: str | None = None) -> str:
    """按键摘取小节，供下游拼提示：键可以是基础小节或「内容」下的小节，按给出的顺序输出，保留原标题；
    文档中没有的键跳过。text 可以带头信息；kind 给出时只识别该类型的「内容」小节。"""
    body = strip_frontmatter(text)
    top = split(body, 2)
    base = types.aliases(types.BASE_SECTIONS)
    content = find(top, types.CONTENT, base) or ""
    content_keys = types.get(kind).sections if kind is not None else tuple(
        key for key in types.HEADINGS if key not in types.BASE_SECTIONS)
    inner = split(content, 3)
    inner_aliases = types.aliases(content_keys)
    parts: list[str] = []
    for key in keys:
        if key in types.BASE_SECTIONS:
            title = next((title for title in top if key_of(title, base) == key), None)
            if title is not None:
                parts.append(f"## {title}\n\n{top[title].strip()}")
            continue
        title = next((title for title in inner if key_of(title, inner_aliases) == key), None)
        if title is not None:
            parts.append(f"### {title}\n\n{inner[title].strip()}")
    return "\n\n".join(parts) + ("\n" if parts else "")
