"""学习建议的决定文档(decision 类型交接文档)：控制措施与改进建议各写一份，进收件箱，由用户批准或拒绝。

选项固定为 accept(批准，用户按「下一步」自己修改配置或应用补丁)与 reject(拒绝并写原因)；接受只记录决定，
程序不修改任何配置、提示或代码。文档写在 data/improve/<建议编号>.md。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path

from tightrein.domain.enums import DocumentStatus
from tightrein.domain.handoff.document import Decision, Event, HandoffDocument, Header, NextStep, Reference
from tightrein.store.files import documents
from tightrein.store.files.layout import WorkspaceLayout

KIND = "decision"
SOURCE = "learn"
TARGET = "user"
ACCEPT, REJECT = "accept", "reject"
SUFFIX = ".md"


@dataclass(frozen=True)
class SuggestionText:
    """conclusion 为一句话的建议；apply 为批准后用户要做的事(配置键与值或应用补丁的命令)。"""

    conclusion: str
    background: str
    accept: str
    reject: str
    recommended: bool
    reason: str
    apply: str
    references: Sequence[Reference] = ()


def write(layout: WorkspaceLayout, suggestion_id: str, subject: str, text: SuggestionText, now: datetime,
          language: str, zone: tzinfo | None) -> Path:
    choice = ACCEPT if text.recommended else REJECT
    options = (f"- `{ACCEPT}`：{text.accept}\n- `{REJECT}`：{text.reject}")
    header = Header(KIND, suggestion_id, DocumentStatus.PENDING, SOURCE, TARGET, subject, now, now,
                    next=f"learn accept {suggestion_id} 或 learn reject {suggestion_id} --reason <原因>")
    document = HandoffDocument(
        header, text.conclusion,
        {"background": text.background, "options": options,
         "recommendation": f"推荐 `{choice}`：{text.reason}"},
        {"options": [{"id": ACCEPT, "summary": text.accept, "recommended": text.recommended},
                     {"id": REJECT, "summary": text.reject, "recommended": not text.recommended}]},
        decisions=(Decision(text.conclusion, "批准" if text.recommended else "拒绝", text.reason),),
        next_steps=(NextStep(f"`tightrein learn accept {suggestion_id}` 记录批准后，{text.apply}", TARGET),
                    NextStep(f"不采纳时 `tightrein learn reject {suggestion_id} --reason <原因>`", TARGET)),
        references=tuple(text.references), history=(Event(now, "生成建议"),))
    path = layout.improve_file(suggestion_id, SUFFIX)
    documents.write(path, document, language, zone)
    return path
