"""原子写：先写同目录的临时文件再改名，中途失败时目标文件保持原样，不留半个文件。"""

from __future__ import annotations

import os
import threading
from pathlib import Path


def write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary(path)
    try:
        temporary.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8", newline="\n")
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary(path)
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _temporary(path: Path) -> Path:
    # 临时文件名带进程号与线程号：多个进程或线程(采集来源并行)同写一个目标时不互相覆盖临时文件
    return path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
