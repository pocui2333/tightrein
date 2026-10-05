"""代码位置的补全(architecture/06 4.6、10.1)：取证输出中的位置应是相对仓库根的完整路径；只写了文件名或缺了前几级
目录时，按代码快照中的路径后缀补全。

- 位置字段(`文件:行号`、`{file, line}` 的 file)与文字中引用的 `文件:行号` 都处理；文件存在时不变；
- 快照中恰好一个文件以该路径结尾时补全为它，没有或有多个时保持原样：位置字段由证据检查判不通过，文字中的引用
  作为一条不通过的原因返回(扩展名在快照中没有出现过的不算代码引用，不报)；
- 分诊在证据检查之前、提 Issue 在渲染正文之前各补全一次，两处共用。
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.evaluation.scorers.code import LOCATION, location_problem

WHOLE = re.compile(r"([^:\s][^:]*):(\d+)(?:-(\d+))?")
SKIPPED_DIRECTORIES = frozenset({".git"})
FILE_KEY = "file"
LINE_KEY = "line"


@dataclass
class Snapshot:
    """代码快照中的文件清单(第一次需要补全时才遍历)。"""

    root: Path
    _files: list[str] | None = None
    unresolved: list[str] = field(default_factory=list)

    def files(self) -> list[str]:
        if self._files is None:
            found = []
            for directory, names, files in os.walk(self.root):
                names[:] = [name for name in names if name not in SKIPPED_DIRECTORIES]
                base = Path(directory).relative_to(self.root)
                found += [(base / name).as_posix() for name in files]
            self._files = found
        return self._files

    def exists(self, file: str) -> bool:
        path = (self.root / file).resolve()
        return self.root.resolve() in path.parents and path.is_file()

    def resolve(self, file: str) -> str | None:
        """完整路径；已存在时原样返回，唯一后缀匹配时补全，否则为 None。"""
        if self.exists(file):
            return file
        # 只去掉开头的「./」；lstrip("./") 会把 .github、.eslintrc.js 开头的点也去掉
        suffix = "/" + re.sub(r"^(\./)+", "", file)
        found = [path for path in self.files() if path.endswith(suffix)]
        return found[0] if len(found) == 1 else None

    def is_code_extension(self, file: str) -> bool:
        extension = Path(file).suffix
        return bool(extension) and any(path.endswith(extension) for path in self.files())


def _whole(snapshot: Snapshot, text: str) -> str | None:
    """整个字符串是一个位置时补全后的值；不是位置时为 None。"""
    match = WHOLE.fullmatch(text)
    if match is None or "/" not in text and "." not in match.group(1):
        return None
    resolved = snapshot.resolve(match.group(1))
    return text if resolved is None else resolved + text[match.end(1):]


def _in_text(snapshot: Snapshot, text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        file = match.group(1)
        resolved = snapshot.resolve(file)
        if resolved is None:
            if snapshot.is_code_extension(file):
                snapshot.unresolved.append(match.group(0))
            return match.group(0)
        whole = resolved + match.group(0)[len(file):]
        problem = location_problem(snapshot.root, whole)
        if problem is not None:
            snapshot.unresolved.append(match.group(0))
        return whole

    return LOCATION.sub(replace, text)


def _complete(snapshot: Snapshot, value: Any) -> Any:
    if isinstance(value, Mapping):
        done = {key: _complete(snapshot, item) for key, item in value.items()}
        file = value.get(FILE_KEY)
        if isinstance(file, str) and isinstance(value.get(LINE_KEY), (int, type(None))):
            done[FILE_KEY] = snapshot.resolve(file) or file
        return done
    if isinstance(value, list):
        return [_complete(snapshot, item) for item in value]
    if isinstance(value, str):
        whole = _whole(snapshot, value)
        return whole if whole is not None else _in_text(snapshot, value)
    return value


@dataclass(frozen=True)
class Completed:
    value: Any
    unresolved: tuple[str, ...]

    def problems(self) -> list[str]:
        return [f"文字中引用的 `{item}` 在代码快照中找不到唯一的文件或行号越界，写相对仓库根的完整路径"
                for item in dict.fromkeys(self.unresolved)]


def complete(value: Any, root: Path, keys: Iterable[str] | None = None) -> Completed:
    """补全 value 中的位置；keys 给出时只处理映射中的这些键(提 Issue 时不动主张与信号原文)。"""
    snapshot = Snapshot(root)
    if keys is not None and isinstance(value, Mapping):
        chosen = set(keys)
        done = {key: _complete(snapshot, item) if key in chosen else item for key, item in value.items()}
    else:
        done = _complete(snapshot, value)
    return Completed(done, tuple(snapshot.unresolved))
