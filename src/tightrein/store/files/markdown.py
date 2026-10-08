"""Markdown 文件：UTF-8、LF、末尾一个空行；写入走原子写。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from tightrein.store.files.atomic import write_text

MAX_LEVEL = 6

_HEADING = re.compile(r"^(#{1,6})(?=\s|$)")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def write_markdown(path: Path, text: str) -> None:
    write_text(path, normalize(text))


def normalize(text: str) -> str:
    """换行统一为 LF，去掉末尾多余的空行，以一个换行结束。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def demote_headings(text: str, levels: int = 1) -> str:
    """把 ATX 标题整体下移 levels 级(嵌进另一份文档的某一节时用)，最多到六级；代码块中的 # 行不动。"""
    if levels < 0:
        raise ValueError(f"下移级数不能为负：{levels}")
    lines = text.split("\n")
    fence: str | None = None
    for index, line in enumerate(lines):
        opening = _FENCE.match(line)
        if opening is not None:
            marker = opening.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        heading = _HEADING.match(line)
        if heading is not None:
            level = min(len(heading.group(1)) + levels, MAX_LEVEL)
            lines[index] = "#" * level + line[len(heading.group(1)):]
    return "\n".join(lines)


FRONT = "---"


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """`---` 包住的 YAML 头信息与其后的正文；没有头信息或 YAML 写坏时抛 ValueError。"""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != FRONT:
        raise ValueError("文件不以 --- 开头，没有头信息")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == FRONT)
    except StopIteration:
        raise ValueError("头信息没有用 --- 结束") from None
    try:
        data = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as error:
        raise ValueError(f"头信息不是合格的 YAML：{error}") from error
    if not isinstance(data, dict):
        raise ValueError("头信息须为键值对")  # noqa: TRY004 调用方按 ValueError 统一处理写坏的头信息
    return data, "\n".join(lines[end + 1:]).lstrip("\n")


def render_frontmatter(data: dict[str, Any], body: str) -> str:
    header = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False).strip()
    return f"{FRONT}\n{header}\n{FRONT}\n\n{body.strip()}\n"
