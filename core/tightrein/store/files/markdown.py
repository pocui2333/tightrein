"""带 YAML frontmatter 的 markdown 读写(design 10.3、16.3)。

文件以 `---` 行开头，frontmatter 到下一个 `---` 行结束，其后全部为正文，正文原样保留。frontmatter 必须是映射。
写入经 atomic.write_text，中途失败时磁盘上不会留下半截文件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.store.files import atomic, yaml_text

DELIMITER = "---"


class FrontmatterError(ValueError):
    """文件没有 frontmatter、frontmatter 没有结束行、无法解析或不是映射。"""


@dataclass(frozen=True)
class MarkdownDocument:
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""


def parse(text: str, source: str = "<text>") -> MarkdownDocument:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != DELIMITER:
        raise FrontmatterError(f"{source} 没有以 {DELIMITER} 开头的 frontmatter")
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == DELIMITER:
            header = "".join(lines[1:index])
            body = "".join(lines[index + 1:])
            break
    else:
        raise FrontmatterError(f"{source} 的 frontmatter 没有结束行 {DELIMITER}")
    try:
        data = yaml_text.load(header)
    except yaml_text.YamlError as error:
        raise FrontmatterError(f"{source} 的 frontmatter 无法解析：{error}") from error
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise FrontmatterError(f"{source} 的 frontmatter 不是映射")
    return MarkdownDocument(data, body)


def render(document: MarkdownDocument) -> str:
    header = yaml_text.dump(document.frontmatter) if document.frontmatter else ""
    return f"{DELIMITER}\n{header}{DELIMITER}\n{document.body}"


def read(path: Path) -> MarkdownDocument:
    return parse(path.read_text(encoding="utf-8"), str(path))


def write(path: Path, document: MarkdownDocument) -> str:
    """写入文件，返回写入的全文。"""
    text = render(document)
    atomic.write_text(path, text)
    return text
