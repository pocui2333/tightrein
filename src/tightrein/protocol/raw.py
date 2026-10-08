"""一个来源在本次运行中的原始输出目录：`data/runs/<运行>/<序号>-<来源>-raw/`。

原始输出的路径一律以相对本目录的 POSIX 路径登记(信号证据中的 `<键>Ref`、探针的标准错误等)；拒绝绝对路径、`..` 与
反斜杠，外部输入(平台返回的名字、探针名)不能把文件写到目录外。保留期与运行目录相同(records.md)。
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from tightrein.protocol.naming import step_sequence
from tightrein.store.files.atomic import write_text
from tightrein.store.files.layout import WorkspaceLayout


def raw_dir(layout: WorkspaceLayout, run: str, source: str) -> Path:
    return layout.run_dir(run) / f"{step_sequence(source):02d}-{source}-raw"


class RawDir:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, relative: str) -> Path:
        pure = PurePosixPath(relative)
        if not relative or pure.is_absolute() or ".." in pure.parts or "\\" in relative:
            raise ValueError(f"原始输出的相对路径不合法：{relative!r}")
        return self.root.joinpath(*pure.parts)

    def write_text(self, relative: str, text: str) -> str:
        write_text(self.path(relative), text)
        return relative

    def write_json(self, relative: str, value: Any) -> str:
        return self.write_text(relative, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def files(self) -> tuple[str, ...]:
        if not self.root.is_dir():
            return ()
        return tuple(sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*") if path.is_file()))
