"""待确认操作的展示、终端确认、confirm 与 reject(architecture/09 4.4)。

终端中当场展示命令、分支与文件、影响、是否影响远程、能否撤销，输入 yes 才同意，同意只对这一次操作有效；
非交互调用不在这里确认，命令以退出码 4 结束，由调用方在用户同意后执行 tightrein approve。
confirm 只执行执行方为 vcs 的操作，执行前由 vcs 复核前置条件与幂等键；前置条件变化时操作改为 expired。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TextIO

from tightrein.cli.assemble import App
from tightrein.domain.enums import OperationStatus
from tightrein.vcs import operations
from tightrein.vcs.executor import OperationResult
from tightrein.vcs.operations import PendingOperation

AGREE = "yes"


def pending_dict(operation: PendingOperation) -> dict[str, Any]:
    return {"id": operation.id, "kind": operation.kind.value, "subjectId": operation.subject_id,
            "description": operations.describe(operation), "command": f"tightrein approve {operation.id}"}


def load(app: App, operation_id: str) -> PendingOperation:
    return operations.load(app.conn, operation_id)


def ask(text: str, stdin: TextIO, stdout: TextIO) -> bool:
    stdout.write(f"{text}\n输入 {AGREE} 同意执行，其他任意输入为不同意：")
    stdout.flush()
    return stdin.readline().strip() == AGREE


def confirm(app: App, operation_id: str) -> OperationResult | PendingOperation:
    """确认一次；达到所需次数后执行。仍需再次确认时返回操作本身。"""
    runner = app.operations()
    confirmed = runner.confirm(operation_id, confirmed_by="user", clock=app.clock)
    if confirmed.status is not OperationStatus.CONFIRMED:
        return confirmed
    return runner.execute(operation_id, clock=app.clock)


def reject(app: App, operation_id: str, note: str | None) -> PendingOperation:
    return app.operations().reject(operation_id, note=note, clock=app.clock)


def terminal(app: App, stdin: TextIO, stdout: TextIO) -> Callable[[str], bool]:
    """continue 在终端中遇到待确认操作时的确认：同意并执行成功返回 True。"""

    def decide(operation_id: str) -> bool:
        operation = load(app, operation_id)
        if not ask(operations.describe(operation), stdin, stdout):
            return False
        result = confirm(app, operation_id)
        return isinstance(result, OperationResult) and result.status is OperationStatus.EXECUTED

    return decide
