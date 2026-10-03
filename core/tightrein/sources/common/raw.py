"""本探针的原始输出目录 data/runs/<运行编号>/raw/<探针>/(architecture/04 1.2)。

原始输出的路径一律以相对本目录的 POSIX 路径登记(ProbeOutcome.artifacts、信号 context 中的 *Ref 与 reportPath)。
写入的相对路径不能以 / 开头，也不能含 `..`，防止外部输入把文件写到目录之外。
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any


class RawDir:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, relative: str) -> Path:
        pure = PurePosixPath(relative)
        if not relative or pure.is_absolute() or ".." in pure.parts or "\\" in relative:
            raise ValueError(f"原始输出的相对路径不合法：{relative!r}")
        return self.root.joinpath(*pure.parts)

    def ensure(self, relative: str | None = None) -> Path:
        directory = self.root if relative is None else self.path(relative)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def write_text(self, relative: str, text: str) -> str:
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return relative

    def write_json(self, relative: str, data: Any) -> str:
        return self.write_text(relative, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def files(self) -> tuple[str, ...]:
        if not self.root.is_dir():
            return ()
        return tuple(sorted(self.relative(path) for path in self.root.rglob("*") if path.is_file()))
