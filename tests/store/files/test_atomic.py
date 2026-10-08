import os
import threading
from pathlib import Path

import pytest

from tightrein.store.files import atomic
from tightrein.store.files.json import read_json, write_json


def test_write_text_creates_parents_and_ends_with_a_newline(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b.md"
    atomic.write_text(path, "第一行")
    assert path.read_bytes() == "第一行\n".encode()
    atomic.write_text(path, "x\n")
    assert path.read_text(encoding="utf-8") == "x\n"


def test_a_failed_atomic_write_keeps_the_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "a.md"
    atomic.write_text(path, "原文")

    def broken(source: object, target: object) -> None:
        raise OSError("磁盘已满")

    monkeypatch.setattr("tightrein.store.files.atomic.os.replace", broken)
    with pytest.raises(OSError):
        atomic.write_text(path, "新内容")
    with pytest.raises(OSError):
        atomic.write_bytes(path, b"new")
    assert path.read_text(encoding="utf-8") == "原文\n"
    assert [item.name for item in tmp_path.iterdir()] == ["a.md"]


def test_the_temporary_file_carries_the_process_and_thread_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 采集并行时同一进程的两个线程可能写同一个文件，临时文件名只带进程号会互相覆盖
    seen: list[str] = []
    real_replace = os.replace

    def spy(source: str, target: str) -> None:
        seen.append(Path(source).name)
        real_replace(source, target)

    monkeypatch.setattr("tightrein.store.files.atomic.os.replace", spy)
    atomic.write_text(tmp_path / "a.json", "{}")
    other = threading.Thread(target=atomic.write_text, args=(tmp_path / "a.json", "{}"))
    other.start()
    other.join()
    assert seen[0] == f".a.json.tmp-{os.getpid()}-{threading.get_ident()}"
    assert len(seen) == 2 and seen[1].startswith(f".a.json.tmp-{os.getpid()}-") and seen[1] != seen[0]


def test_mode_is_applied(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    atomic.write_text(path, "{}", mode=0o600)
    assert path.stat().st_mode & 0o777 == 0o600


def test_json_is_utf8_indented_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "handoff.json"
    write_json(path, {"summary": "已修复", "facts": {"files": 2}})
    assert path.read_text(encoding="utf-8") == '{\n  "summary": "已修复",\n  "facts": {\n    "files": 2\n  }\n}\n'
    assert read_json(path) == {"summary": "已修复", "facts": {"files": 2}}
