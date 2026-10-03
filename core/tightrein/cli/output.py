"""命令的输出(architecture/09 4.3)。

人读：第一行是结论，随后是明细，停止时最后一行是下一步命令。JSON：标准输出只写一个对象，字段为 command、status、
exitCode、subject、result、stoppedAt、next、pendingOperations、errors；日志与进度写到标准错误。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, TextIO

from tightrein.cli import exit_codes


@dataclass
class Outcome:
    command: str
    exit_code: int = exit_codes.OK
    lines: list[str] = field(default_factory=list)
    subject: dict[str, Any] | None = None
    result: Any = None
    stopped_at: dict[str, Any] | None = None
    next: str | None = None
    pending: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"command": self.command, "status": exit_codes.status_text(self.exit_code),
                "exitCode": self.exit_code, "subject": self.subject, "result": self.result,
                "stoppedAt": self.stopped_at, "next": self.next, "pendingOperations": self.pending,
                "errors": self.errors}


def error(kind: str, message: str, hint: str | None = None) -> dict[str, Any]:
    return {"type": kind, "message": message, "hint": hint}


def emit(outcome: Outcome, json_mode: bool, stdout: TextIO) -> None:
    if json_mode:
        stdout.write(json.dumps(outcome.to_dict(), ensure_ascii=False, default=str) + "\n")
        return
    lines = list(outcome.lines)
    for item in outcome.errors:
        lines.append(f"错误({item['type']})：{item['message']}" + (f"；{item['hint']}" if item.get("hint") else ""))
    for operation in outcome.pending:
        lines.append(operation["description"])
        lines.append(f"同意后执行：tightrein confirm {operation['id']}；拒绝：tightrein reject {operation['id']}")
    if outcome.next:
        lines.append(f"下一步：{outcome.next}")
    stdout.write("\n".join(lines) + ("\n" if lines else ""))
