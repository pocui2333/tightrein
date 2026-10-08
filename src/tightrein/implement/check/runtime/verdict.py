"""本机运行检查的单项结果(三档结论加失败)。

| 结果 | 含义 |
|---|---|
| passed | 执行了且满足通过条件，附证据(命令、输出、截图) |
| weak | 执行了，但前置条件不满足或用例因数据缺失被跳过：说清为什么弱 |
| unverified | 没有执行：逐条写出本应验证的行为与原因 |
| failed | 执行了但不满足通过条件 |

弱证据写成通过等同伪造检查通过；未验证与弱证据写进报告，不阻断，只有失败才是不通过项。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Result(StrEnum):
    PASSED = "passed"
    WEAK = "weak"
    UNVERIFIED = "unverified"
    FAILED = "failed"


@dataclass(frozen=True)
class Item:
    id: str  # api:<方法 路由>、page:<路径>、screenshot:<文件>、migration
    category: str  # api、pages、screenshots、migration、local_run
    result: Result
    command: str | None = None
    evidence: tuple[str, ...] = ()
    reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "category": self.category, "result": self.result.value, "command": self.command,
                "evidence": list(self.evidence), "reason": self.reason}


def unverified(id: str, category: str, reason: str) -> Item:
    return Item(id, category, Result.UNVERIFIED, reason=reason)


def failed(items: Sequence[Item]) -> list[Item]:
    return [item for item in items if item.result is Result.FAILED]
