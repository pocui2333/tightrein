"""JSON 文件：UTF-8、两个空格缩进、LF、末尾一个空行；写入走原子写。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tightrein.store.files.atomic import write_text


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def write_json(path: Path, value: Any, *, mode: int | None = None) -> None:
    write_text(path, dumps(value), mode=mode)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
