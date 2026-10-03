"""复现检查清单 regressions/<Issue 编号>/check.yaml 的读取、校验与哈希(architecture/04 7.1)。

清单按 data/regression.schema.json 校验，所引用的文件都须存在：接口类为 `<检查编号>.request.json`(清单的 file)与
`<检查编号>.expect.json`，可选的前置条件请求为 `<检查编号>.precondition.json`；页面类为 Playwright 用例；静态类为
Semgrep 规则；测试类的 file 是测试文件相对仓库根的路径(不得为绝对路径或含 ..)，登记副本为目录下的
`<检查编号><后缀>`，由核心放进修复 worktree 的 file 处执行(place_tests；内容与登记副本一致的为 placed)。每条检查的哈希为清单条目加所引用文件内容的 sha256，
写入时登记在 regressions 表，每次执行前核对，不一致说明复现检查被改动，直接报错。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from tightrein.contracts import validate
from tightrein.domain.enums import RegressionKind
from tightrein.store.files import atomic, yaml_text

SCHEMA = "data/regression.schema.json"
CHECKLIST = "check.yaml"
EXPECT_SUFFIX = ".expect.json"
PRECONDITION_SUFFIX = ".precondition.json"


class ManifestInvalid(Exception):
    """清单不合格：缺字段、文件不存在或无法解析；该 Issue 的检查记为无法执行。"""


class ManifestTampered(Exception):
    """清单条目或所引用的文件与登记的哈希不一致。"""


@dataclass(frozen=True)
class CheckEntry:
    id: str
    kind: RegressionKind
    file: str
    location: str
    requires: tuple[str, ...] = ()
    role: str | None = None
    targets: tuple[str, ...] = ()
    precondition: str | None = None
    command: str | None = None
    expected_signature: str | None = None  # 预期异常签名：基准版本上失败时核对输出包含该签名才算有效复现

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CheckEntry:
        return cls(data["id"], RegressionKind(data["kind"]), data["file"], data["location"],
                   tuple(data.get("requires", ())), data.get("role"), tuple(data.get("targets", ())),
                   data.get("precondition"), data.get("command"), data.get("expectedSignature"))

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "kind": self.kind.value, "role": self.role, "file": self.file,
                                "location": self.location, "requires": list(self.requires),
                                "precondition": self.precondition}
        if self.targets:
            data["targets"] = list(self.targets)
        if self.command is not None:
            data["command"] = self.command
        if self.expected_signature is not None:
            data["expectedSignature"] = self.expected_signature
        return data

    @property
    def stored_name(self) -> str:
        """检查文件在复现检查目录中的文件名：测试类为登记副本 `<检查编号><后缀>`，其余为 file。"""
        if self.kind is RegressionKind.TEST:
            return f"{self.id}{PurePosixPath(self.file).suffix}"
        return self.file

    def files(self) -> tuple[str, ...]:
        """所引用的文件：接口类另有期望文件，前置条件请求文件存在时一并计入。"""
        if self.kind is RegressionKind.API:
            return (self.file, f"{self.id}{EXPECT_SUFFIX}")
        return (self.stored_name,)

    def code_paths(self) -> tuple[str, ...]:
        """检查针对的代码文件(相对仓库根)：静态类为 targets，测试类为 location 中的路径；用于判断与改动是否相关。"""
        if self.kind is RegressionKind.STATIC:
            return self.targets
        if self.kind is RegressionKind.TEST:
            path, _, line = self.location.rpartition(":")
            return (path if path and line.isdigit() else self.location,)
        return ()


def unsafe_path(path: str) -> bool:
    """测试类的 file 须为仓库内的相对路径。"""
    pure = PurePosixPath(path)
    return pure.is_absolute() or ".." in pure.parts or "\\" in path


@dataclass(frozen=True)
class Manifest:
    issue: str
    problems: tuple[str, ...]
    checks: tuple[CheckEntry, ...]

    def entry(self, check_id: str) -> CheckEntry | None:
        return next((item for item in self.checks if item.id == check_id), None)


def parse(data: Any, directory: Path) -> Manifest:
    errors = validate.validate(SCHEMA, data)
    if errors:
        raise ManifestInvalid("；".join(str(error) for error in errors))
    checks = tuple(CheckEntry.from_dict(item) for item in data["checks"])
    manifest = Manifest(data["issue"], tuple(data["problems"]), checks)
    unsafe = [entry.id for entry in checks if entry.kind is RegressionKind.TEST and unsafe_path(entry.file)]
    if unsafe:
        raise ManifestInvalid(f"测试类检查的 file 须为仓库内的相对路径：{'、'.join(unsafe)}")
    missing = [name for entry in manifest.checks for name in entry.files() if not (directory / name).is_file()]
    if missing:
        raise ManifestInvalid(f"清单引用的文件不存在：{'、'.join(missing)}")
    return manifest


def load(directory: Path) -> Manifest:
    path = directory / CHECKLIST
    if not path.is_file():
        raise ManifestInvalid(f"没有清单 {path}")
    try:
        data = yaml_text.load(path.read_text(encoding="utf-8"))
    except yaml_text.YamlError as error:
        raise ManifestInvalid(f"{path} 无法解析：{error}") from error
    return parse(data, directory)


def entry_hash(directory: Path, entry: CheckEntry) -> str:
    digest = hashlib.sha256(json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8"))
    names = [*entry.files()]
    if (directory / f"{entry.id}{PRECONDITION_SUFFIX}").is_file():
        names.append(f"{entry.id}{PRECONDITION_SUFFIX}")
    for name in names:
        digest.update(b"\0" + name.encode("utf-8") + b"\0")
        digest.update((directory / name).read_bytes())
    return digest.hexdigest()


def verify(directory: Path, entry: CheckEntry, expected: str) -> None:
    actual = entry_hash(directory, entry)
    if actual != expected:
        raise ManifestTampered(f"复现检查 {directory.name}/{entry.id} 与登记的哈希不一致，检查或其文件被改动过")


def repro_tests(directory: Path) -> dict[str, str]:
    """本 Issue 测试类检查的测试文件：仓库路径 → 登记副本的内容；没有清单或清单不合格时为空。"""
    try:
        loaded = load(directory)
    except ManifestInvalid:
        return {}
    return {entry.file: (directory / entry.stored_name).read_text(encoding="utf-8") for entry in loaded.checks
            if entry.kind is RegressionKind.TEST}


def placed(directory: Path, worktree: Path) -> set[str]:
    """worktree 中内容与登记副本一致的复现测试。"""
    return {path for path, text in repro_tests(directory).items()
            if (worktree / path).is_file() and (worktree / path).read_text(encoding="utf-8") == text}


def place_tests(directory: Path, worktree: Path) -> list[str]:
    """把登记副本写到 worktree 的 file 处，返回不存在或内容不同而被写入的路径。"""
    written = []
    for path, text in repro_tests(directory).items():
        target = worktree / path
        if target.is_file() and target.read_text(encoding="utf-8") == text:
            continue
        atomic.write_text(target, text)
        written.append(path)
    return written
