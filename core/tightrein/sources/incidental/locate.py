"""从发现原文中提取位置(architecture/04 6.3)：`文件路径:行号` 与 `类名.方法名`。

- 文件：带小写扩展名、文件名至少两个字符的路径(可在反引号中)，后面可跟 `:行号` 或 `:行号-行号`；取原文中第一个；
- 符号：原文中第一个「大写开头的标识符.大写开头的标识符」(C# 的类名与方法名)，文件名中的不算；
- 提取不到文件时返回空，由调用方计入 stats.unlocated，由用户手动补登。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FILE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w-]{2,}(?:\.[\w-]+)*\.[a-z][a-z0-9]{0,9})(?![\w])(?::(\d+)(?:-\d+)?)?")
SYMBOL = re.compile(r"\b([A-Z][A-Za-z0-9_]*\.[A-Z][A-Za-z0-9_]*)\b")


@dataclass(frozen=True)
class Location:
    file: str
    line: int | None
    symbol: str | None

    def text(self) -> str:
        return f"{self.file}:{self.symbol}" if self.symbol else self.file


def locate(text: str) -> Location | None:
    match = FILE.search(text)
    if match is None:
        return None
    file = match.group(1)
    line = int(match.group(2)) if match.group(2) else None
    name = file.rsplit("/", 1)[-1]
    symbol = next((found.group(1) for found in SYMBOL.finditer(text) if found.group(1) not in name), None)
    return Location(file, line, symbol)
