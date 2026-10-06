"""Markdown 交接文档的读写与校验(redesign/00-handoff-documents.md 第 1、4 节)。

文件为 YAML 头信息(按类型的 header_schema，通常为 handoff/document.schema.json)加正文；正文由 domain/handoff/document.render_body 按类型模板渲染，
数据块写成 YAML。历史只追加：append_history 在「历史」末尾加一行并更新头信息的 updated，状态只改头信息。
check 返回全部问题(空列表表示通过)，不抛异常，供 `tightrein admin doc check` 与交接前的校验使用。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from tightrein.contracts.validate import validate
from tightrein.domain.clock import format_iso
from tightrein.domain.enums import DocumentStatus
from tightrein.domain.handoff import sections, types
from tightrein.domain.handoff.document import HandoffDocument, render_body
from tightrein.store.files import atomic, markdown, yaml_text
from tightrein.store.files.markdown import FrontmatterError, MarkdownDocument

DEFAULT_LIMIT = "default"


class DocumentError(ValueError):
    """文档无法读取：没有头信息、头信息不合格或类型未登记。"""


@dataclass(frozen=True)
class ParsedDocument:
    """读回的文档：sections 为「内容」下各小节的文字(键为小节键)，blocks 为解析后的数据块。"""

    header: dict[str, Any]
    body: str
    conclusion: str
    sections: dict[str, str]
    blocks: dict[str, Any]

    @property
    def status(self) -> DocumentStatus:
        return DocumentStatus(self.header["status"])


def _dump(data: Any) -> str:
    return yaml_text.dump(data)


def render(document: HandoffDocument, code: str, zone: tzinfo | None = None) -> str:
    return markdown.render(MarkdownDocument(document.header.to_dict(), render_body(document, code, _dump, zone)))


def write(path: Path, document: HandoffDocument, code: str, zone: tzinfo | None = None) -> str:
    """渲染并写入；写入前按 check 校验(不含长度上限)，不合格时不落盘。返回写入的全文。"""
    text = render(document, code, zone)
    problems = check(text)
    if problems:
        raise DocumentError(f"{path.name} 不合格：" + "；".join(problems))
    atomic.write_text(path, text)
    return text


def parse(text: str, source: str = "<text>") -> ParsedDocument:
    try:
        raw = markdown.parse(text, source)
    except FrontmatterError as error:
        raise DocumentError(str(error)) from error
    try:
        doc_type = types.get(str(raw.frontmatter.get("kind")))
    except types.UnknownKind as error:
        raise DocumentError(f"{source}：{error}") from error
    errors = validate(doc_type.header_schema, raw.frontmatter)
    if errors:
        raise DocumentError(f"{source} 的头信息不符合 {doc_type.header_schema}：" +
                            "；".join(str(error) for error in errors))
    top = sections.split(raw.body, 2)
    base = types.aliases(types.BASE_SECTIONS)
    content = sections.find(top, types.CONTENT, base) or ""
    inner = sections.split(content, 3)
    inner_aliases = types.aliases(doc_type.sections)
    found = {key: sections.without_blocks(part).strip() for key in doc_type.sections
             if (part := sections.find(inner, key, inner_aliases)) is not None}
    blocks: dict[str, Any] = {}
    for label, raw_block in sections.blocks(raw.body).items():
        try:
            blocks[label] = yaml_text.load(raw_block)
        except yaml_text.YamlError as error:
            raise DocumentError(f"{source} 的数据块 {label} 无法解析：{error}") from error
    return ParsedDocument(raw.frontmatter, raw.body, sections.find(top, types.CONCLUSION, base) or "", found, blocks)


def read(path: Path) -> ParsedDocument:
    return parse(path.read_text(encoding="utf-8"), str(path))


def append_history(path: Path, at: datetime, event: str, status: DocumentStatus | None = None,
                   zone: tzinfo | None = None) -> str:
    """在「历史」末尾追加一行(原为「无」时取代它)，更新头信息的 updated；给出 status 时同时改状态。
    其余内容不变，返回写入的全文。"""
    document = parse(path.read_text(encoding="utf-8"), str(path))
    base = types.aliases(types.BASE_SECTIONS)
    body = document.body
    title = sections.title_in(body, types.HISTORY, base)
    if title is None:
        raise DocumentError(f"{path.name} 没有「历史」小节")
    line = sections.history_line(at, event, zone)
    if sections.find(sections.split(body, 2), types.HISTORY, base) in types.TEXTS["none"].values():
        start = body.index(f"## {title}")
        following = re.compile(r"^## ", re.MULTILINE).search(body, start + 1)
        rest = body[following.start():] if following is not None else ""
        body = body[:start] + f"## {title}\n\n{line}\n" + (f"\n{rest}" if rest else "")
    else:
        body = sections.append_line(body, title, line)
    header = {**document.header, "updated": format_iso(at)}
    if status is not None:
        header["status"] = status.value
    return markdown.write(path, MarkdownDocument(header, body))


def check(text: str, limits: Mapping[str, int] | None = None, source: str = "<text>") -> list[str]:
    """校验头信息、基础小节(齐全且按顺序)、类型的必需小节、数据块(标签已知、必需的存在、符合 schema)；
    limits 给出时再检查各小节去掉数据块后的字符数。"""
    try:
        document = parse(text, source)
    except DocumentError as error:
        return [str(error)]
    doc_type = types.get(document.header["kind"])
    base = types.aliases(types.BASE_SECTIONS)
    problems: list[str] = []
    present = sections.keys_in(document.body, base)
    for key in types.BASE_SECTIONS:
        if key not in present:
            problems.append(f"缺少小节「{types.heading(key, 'zh')}」")
    ordered = [key for key in present if key in types.BASE_SECTIONS]
    if not problems and ordered != list(types.BASE_SECTIONS):
        problems.append("基础小节的顺序应为：" + "、".join(types.heading(key, "zh") for key in types.BASE_SECTIONS))
    if not document.conclusion.strip():
        problems.append("「结论」为空")
    for key in doc_type.sections:
        if not document.sections.get(key):
            problems.append(f"缺少「内容」小节「{types.heading(key, 'zh')}」")
    raw_blocks = sections.blocks(document.body)
    for label in sorted(set(raw_blocks) - set(doc_type.blocks)):
        problems.append(f"类型 {doc_type.kind} 没有数据块 {label}")
    for label in sorted(doc_type.required_blocks - set(raw_blocks)):
        problems.append(f"缺少数据块 {label}")
    for label in sorted(set(document.blocks) & set(doc_type.blocks)):
        problems += [f"数据块 {label} {error}" for error in validate(doc_type.schema, document.blocks[label], label)]
    if limits is not None:
        problems += _length_problems(document, limits)
    return problems


def _length_problems(document: ParsedDocument, limits: Mapping[str, int]) -> list[str]:
    base = types.aliases(types.BASE_SECTIONS)
    top = sections.split(document.body, 2)
    measured = {key: sections.without_blocks(sections.find(top, key, base) or "").strip()
                for key in types.BASE_SECTIONS if key != types.CONTENT}
    measured.update(document.sections)
    problems = []
    for key, value in measured.items():
        limit = limits.get(key, limits[DEFAULT_LIMIT])
        if len(value) > limit:
            problems.append(f"「{types.heading(key, 'zh')}」有 {len(value)} 个字符，超过上限 {limit}；"
                            "超出的部分放进单独的附件文件，正文只给引用与摘要")
    return problems
