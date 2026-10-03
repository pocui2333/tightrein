"""Issue 文件的正文(redesign/04-issue.md)：交接文档 issue 类型的版式，由 domain/handoff/document.render_parts 渲染；
以及历史行与「引用」的追加。

正文的六个基础小节与「内容」下的八个小节按 project.language 取标题。用户需求的「问题」是用户原文：原文中的验收标准
提到「验收标准」一节，其余标题降为加粗文字，避免打乱小节结构；其余小节写「—」。修改已有文件时只追加「历史」与
「引用」(旧版式为「关联」)，其余正文保持用户编辑后的内容；历史行写绝对日期与时区。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, tzinfo

from tightrein.domain import issue_sections
from tightrein.domain.handoff import sections, types
from tightrein.domain.handoff.document import NextStep, Reference, render_parts
from tightrein.domain.handoff.sections import history_line
from tightrein.domain.issue_sections import ACCEPTANCE, CONTENT_KEYS, HISTORY, PROBLEM, REFERENCES, RELATED
from tightrein.pipeline.issue.render import labels
from tightrein.pipeline.issue.render.labels import MISSING
from tightrein.pipeline.issue.steps import body as body_step

LEGACY_LANGUAGE = "zh"  # 认不出正文语言时按旧文件的中文标题
KIND = "issue"
HEADING_LINE = re.compile(r"^#{1,6} +(.+?)\s*$", re.MULTILINE)
OWNER_USER = "user"


def document(conclusion: str, content: Mapping[str, str], references: Sequence[Reference],
             next_steps: Sequence[NextStep], history: str, language: str) -> str:
    """按交接文档版式渲染正文；history 为已写好的历史行(保留原有历史时原样传入)。"""
    return render_parts(KIND, conclusion, content, language, str, next_steps=tuple(next_steps),
                        references=tuple(references), history=history)


def required_sections() -> tuple[str, ...]:
    """必需小节的键：「内容」下的八个小节与历史。"""
    return (*CONTENT_KEYS, HISTORY)


def approve_step(issue_id: str, language: str) -> NextStep:
    return NextStep(labels.text("next.approve", language, n=issue_id.lstrip("0") or "0"), OWNER_USER)


def fix_step(issue_id: str, language: str) -> NextStep:
    return NextStep(labels.text("next.fix", language, n=issue_id.lstrip("0") or "0"), OWNER_USER)


def _demote(text: str) -> str:
    return HEADING_LINE.sub(lambda match: f"**{match.group(1)}**", text)


def requirement_parts(requirement: str) -> tuple[str, list[str]]:
    """用户原文拆成「问题」的正文与其中验收标准一节的条目(以「- 」开头的行，去掉复选框)。"""
    kept: list[str] = []
    criteria: list[str] = []
    in_acceptance = False
    for line in requirement.splitlines():
        if line.startswith("## "):
            in_acceptance = issue_sections.key_of(line[3:]) == ACCEPTANCE
            if in_acceptance:
                continue
        if not in_acceptance:
            kept.append(line)
        elif line.startswith("- "):
            item = re.sub(r"^\[[ xX]\]\s*", "", line[2:].strip())
            if item:
                criteria.append(item)
    return _demote("\n".join(kept).strip()), criteria


def manual_content(requirement: str, language: str, repro_test: bool = True) -> dict[str, str]:
    """用户需求的「内容」：问题为原文，验收标准为固定条目加原文中的条目(没有时附说明)，其余为「—」。"""
    text, criteria = requirement_parts(requirement)
    acceptance = body_step.checkboxes([*body_step.fixed_acceptance([], repro_test, language), *criteria])
    if not criteria:
        acceptance += f"\n\n{labels.text('manualAcceptance', language)}"
    content = {key: MISSING for key in CONTENT_KEYS}
    content.update({PROBLEM: text or MISSING, ACCEPTANCE: acceptance})
    return content


def manual_document(issue_id: str, title: str, requirement: str, history: str, language: str,
                    references: Sequence[Reference] = (), repro_test: bool = True) -> str:
    return document(title, manual_content(requirement, language, repro_test), references,
                    [fix_step(issue_id, language)], history, language)


def _language(body: str) -> str:
    return sections.language_of(body, issue_sections.HEADINGS, LEGACY_LANGUAGE)


def _append(body: str, key: str, line: str) -> str:
    """在正文中这一节(任一语言的标题)末尾追加一行，原为「无」时取代它；没有这一节时按正文已有标题的语言在文末新建。"""
    title = issue_sections.title_in(body, key) or issue_sections.heading(key, _language(body))
    current = sections.find(sections.split(body), key, issue_sections.ALIASES)
    if current is not None and current.strip() in types.TEXTS["none"].values():
        return body.replace(f"## {title}\n\n{current.strip()}", f"## {title}\n\n{line}", 1)
    return sections.append_line(body, title, line)


def append_history(body: str, at: datetime, text: str, zone: tzinfo | None = None) -> str:
    return _append(body, HISTORY, history_line(at, text, zone))


def append_history_line(body: str, line: str) -> str:
    return _append(body, HISTORY, line)


def append_related(body: str, problem_id: str, title: str) -> str:
    """把关联问题追加到「引用」(旧版式为「关联」)。"""
    if issue_sections.is_handoff(body):
        return _append(body, REFERENCES, f"- `{problem_id}` {title}".rstrip())
    return _append(body, RELATED, f"- {problem_id} {title}".rstrip())
