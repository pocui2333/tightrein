"""交接文档的结构化内容与正文渲染。

模型只负责内容(结论、各小节的分析文字、数据块的数据、需要决定的问题等)，程序按类型模板写出正文：六个基础小节、
「内容」下的固定小节、数据块、引用与历史，小节标题按 project.language。数据块的文本由调用方序列化后传入
(store/files/documents.py 写成 YAML)，这里不依赖具体格式。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any

from tightrein.domain.clock import format_iso
from tightrein.domain.enums import DocumentStatus
from tightrein.domain.handoff import sections, types
from tightrein.domain.handoff.types import CONCLUSION, CONTENT, DECISIONS, HISTORY, NEXT, REFERENCES

STRUCTURE_LEVELS = 3  # 文档自身用 1 到 3 级标题(基础小节与「内容」下的小节)
MAX_HEADING_LEVEL = 6
_HEADING = re.compile(r"^(#{1,6})(?=\s)")


def demote_headings(text: str) -> str:
    """模型写的自由文字中的标题整体下移，不与文档自身的小节标题混淆；代码块中的行不动。"""
    lines, in_fence = [], False
    for line in text.split("\n"):
        if sections.FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            line = _HEADING.sub(lambda found: "#" * min(len(found.group(1)) + STRUCTURE_LEVELS, MAX_HEADING_LEVEL),
                                line)
        lines.append(line)
    return "\n".join(lines)


@dataclass(frozen=True)
class Header:
    """头信息，给程序路由、判断状态与串联链路；状态只看这里。"""

    kind: str
    id: str
    status: DocumentStatus
    source: str
    target: str
    subject: str
    created: datetime
    updated: datetime
    parent: str | None = None
    next: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "status": self.status.value, "from": self.source, "to": self.target,
                "subject": self.subject, "parent": self.parent, "created": format_iso(self.created),
                "updated": format_iso(self.updated), "next": self.next}


@dataclass(frozen=True)
class Decision:
    question: str
    recommendation: str
    reason: str


@dataclass(frozen=True)
class NextStep:
    text: str
    owner: str
    done: bool = False


@dataclass(frozen=True)
class Reference:
    path: str
    note: str = ""


@dataclass(frozen=True)
class Event:
    at: datetime
    text: str


@dataclass(frozen=True)
class HandoffDocument:
    """一份交接文档的全部内容：sections 为「内容」下各小节的文字(键为小节键)，blocks 为数据块的数据(键为标签)。"""

    header: Header
    conclusion: str
    sections: Mapping[str, str]
    blocks: Mapping[str, Any] = field(default_factory=dict)
    decisions: tuple[Decision, ...] = ()
    next_steps: tuple[NextStep, ...] = ()
    references: tuple[Reference, ...] = ()
    history: tuple[Event, ...] = ()


class RenderError(ValueError):
    """内容缺少类型要求的小节或数据块，或带了类型没有的数据块。"""


def render_body(document: HandoffDocument, code: str, dump: Callable[[Any], str],
                zone: tzinfo | None = None) -> str:
    """按类型模板渲染正文；dump 把数据块的数据序列化为代码块中的文本。"""
    history = [sections.history_line(event.at, event.text, zone) for event in document.history]
    return render_parts(document.header.kind, document.conclusion, document.sections, code, dump,
                        blocks=document.blocks, decisions=document.decisions, next_steps=document.next_steps,
                        references=document.references, history="\n".join(history))


def render_parts(kind: str, conclusion: str, content_sections: Mapping[str, str], code: str,
                 dump: Callable[[Any], str], *, blocks: Mapping[str, Any] | None = None,
                 decisions: tuple[Decision, ...] = (), next_steps: tuple[NextStep, ...] = (),
                 references: tuple[Reference, ...] = (), history: str = "") -> str:
    """按类型模板渲染六个基础小节；history 为已写好的历史行(保留原有历史时原样传入)。"""
    doc_type = types.get(kind)
    blocks = blocks or {}
    missing = [key for key in doc_type.sections if not content_sections.get(key, "").strip()]
    unknown = sorted(set(content_sections) - set(doc_type.sections))
    blocks_missing = sorted(doc_type.required_blocks - set(blocks))
    blocks_unknown = sorted(set(blocks) - set(doc_type.blocks))
    problems = [*(f"缺少小节 {key}" for key in missing), *(f"类型 {doc_type.kind} 没有小节 {key}" for key in unknown),
                *(f"缺少数据块 {label}" for label in blocks_missing),
                *(f"类型 {doc_type.kind} 没有数据块 {label}" for label in blocks_unknown)]
    if problems:
        raise RenderError("；".join(problems))
    content = []
    for key in doc_type.sections:
        part = demote_headings(content_sections[key].strip())
        for label, section in doc_type.blocks.items():
            if section == key and label in blocks:
                part += f"\n\n```yaml data:{label}\n{dump(blocks[label]).rstrip()}\n```"
        content.append(f"### {types.heading(key, code)}\n\n{part}")
    none = types.text("none", code)
    decided = [f"- {item.question} —— {types.text('recommendation', code)}：{item.recommendation}；"
               f"{types.text('reason', code)}：{item.reason}" for item in decisions]
    steps = [f"- [{'x' if step.done else ' '}] {step.text}({step.owner})" for step in next_steps]
    cited = [f"- `{item.path}` {item.note}".rstrip() for item in references]
    parts = {
        CONCLUSION: demote_headings(conclusion.strip()),
        CONTENT: "\n\n".join(content),
        DECISIONS: "\n".join(decided) or none,
        NEXT: "\n".join(steps) or none,
        REFERENCES: "\n".join(cited) or none,
        HISTORY: history.strip() or none,
    }
    return "\n\n".join(f"## {types.heading(key, code)}\n\n{parts[key]}" for key in types.BASE_SECTIONS) + "\n"
