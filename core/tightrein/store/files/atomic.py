"""原子写入：先写同目录下的临时文件，再改名替换目标文件，中途失败时目标文件保持原样，不留下半截文件。"""

from __future__ import annotations

import os
from pathlib import Path


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
