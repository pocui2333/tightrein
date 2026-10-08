"""外部 skills 的锁定、校验与更新(vendor/README.md；protocol/security.md「外部代码」)。

- 锁定清单 `vendor/lock.json`：每个 skill 的来源仓库、完整 commit、在来源仓库中的路径、许可证、目录哈希与逐文件 sha256；
- 目录哈希：全部文件(不含 .git)按相对路径排序，逐行拼接「相对路径、制表符、内容的 sha256、换行」后求 sha256，
  不计权限与修改时间；
- 加载前逐文件核对，任一不符即不加载，报出文件与期望、实际哈希；
- 下载：按完整 commit 取来源仓库的源码归档(不执行 git)，只解出该 skill 目录的普通文件，先解到 `.partial` 再改名；
  已有的副本被改过时不重新下载覆盖，刚下载的与清单不符立即删除。
下载由调用方注入(程序对外访问只去登记过的地址，见 protocol/security.md「网络」)。
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import tarfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from tightrein.protocol.naming import segment
from tightrein.store.files.json import read_json, write_json
from tightrein.store.files.layout import ToolLayout

LOCK_VERSION = 1
HASH_PREFIX = "sha256:"
GITHUB_PREFIXES = ("https://github.com/", "git@github.com:")
ARCHIVE_URL = "https://codeload.github.com/{slug}/tar.gz/{commit}"
COMMIT = re.compile(r"^[0-9a-f]{40}$")
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKIP_DIRECTORY = ".git"
PARTIAL_SUFFIX = ".partial"
SKILL_FILE = "SKILL.md"
SKILL_KEYS = frozenset({"name", "source", "commit", "path", "license", "treeHash", "files", "lockedAt"})
LOCKED_KEYS = ("commit", "path", "license", "treeHash", "files", "lockedAt")

Fetch = Callable[[str, str], bytes]
"""(来源仓库, commit) → 源码归档(tar.gz)的内容。"""


class VendorError(Exception):
    """锁定、下载或校验失败。"""


class LockInvalid(VendorError, ValueError):
    """锁定清单不合格；problems 一次列出全部。"""

    def __init__(self, path: Path, problems: Sequence[str]) -> None:
        super().__init__(f"{path}：" + "；".join(problems))
        self.problems = list(problems)


@dataclass(frozen=True)
class FileMismatch:
    name: str
    path: str
    expected: str | None
    actual: str | None

    def describe(self) -> str:
        return f"{self.name} 的 {self.path}：期望 {self.expected or '不存在'}，实际 {self.actual or '不存在'}"


class HashMismatch(VendorError):
    def __init__(self, mismatches: Sequence[FileMismatch]) -> None:
        self.mismatches = list(mismatches)
        super().__init__("哈希不符，不加载：" + "；".join(item.describe() for item in mismatches))


@dataclass(frozen=True)
class LockedSkill:
    name: str
    source: str
    commit: str | None = None
    path: str | None = None
    license: str | None = None
    tree_hash: str | None = None
    files: dict[str, str] = field(default_factory=dict)  # 相对路径 → sha256(十六进制)
    locked_at: str | None = None

    @property
    def locked(self) -> bool:
        return self.commit is not None

    def to_json(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            "name": self.name, "source": self.source, "commit": self.commit, "path": self.path,
            "license": self.license, "treeHash": self.tree_hash,
            "files": dict(sorted(self.files.items())) if self.locked else None, "lockedAt": self.locked_at,
        }
        return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True)
class LockChange:
    name: str
    old_commit: str | None
    new_commit: str
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]

    def describe(self) -> str:
        parts = [f"{label} {', '.join(items)}" for label, items in
                 (("新增", self.added), ("删除", self.removed), ("改动", self.changed)) if items]
        head = f"{self.name}：{self.old_commit or '未锁定'} → {self.new_commit}"
        return head + (f"(文件：{'；'.join(parts)})" if parts else "")


# 锁定清单


def read_lock(path: Path) -> list[LockedSkill]:
    """读取锁定清单；文件不存在时为空。"""
    if not path.is_file():
        return []
    data = read_json(path)
    problems = _lock_problems(data)
    if problems:
        raise LockInvalid(path, problems)
    return [_skill(item) for item in data["skills"]]


def write_lock(path: Path, skills: Sequence[LockedSkill]) -> None:
    write_json(path, {"lockVersion": LOCK_VERSION, "skills": [skill.to_json() for skill in skills]})


def find(skills: Sequence[LockedSkill], name: str) -> LockedSkill:
    for skill in skills:
        if skill.name == name:
            return skill
    raise VendorError(f"锁定清单中没有 {name}")


# 哈希与校验


def file_hashes(directory: Path) -> dict[str, str]:
    """目录下全部文件(跟随链接，不含 .git)的相对路径与 sha256，按相对路径排序。"""
    found: dict[str, str] = {}
    for folder, directories, names in os.walk(directory, followlinks=True):
        directories[:] = [name for name in directories if name != SKIP_DIRECTORY]
        for name in names:
            path = Path(folder) / name
            found[path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(found.items()))


def tree_hash(files: dict[str, str]) -> str:
    text = "".join(f"{path}\t{digest}\n" for path, digest in sorted(files.items()))
    return HASH_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify(skill: LockedSkill, directory: Path) -> list[FileMismatch]:
    """按清单逐文件核对目录；目录不存在时报一项。"""
    if not directory.is_dir():
        return [FileMismatch(skill.name, str(directory), skill.tree_hash, None)]
    actual = file_hashes(directory)
    return [FileMismatch(skill.name, path, skill.files.get(path), actual.get(path))
            for path in sorted(set(skill.files) | set(actual)) if skill.files.get(path) != actual.get(path)]


def load(tool: ToolLayout, name: str) -> Path:
    """调用点加载一个 skill 前调用：已锁定且逐文件核对通过才返回目录，否则抛出(不加载)。"""
    skill = find(read_lock(tool.vendor_lock), name)
    if not skill.locked:
        raise VendorError(f"{name} 尚未锁定，不加载")
    directory = tool.vendor_skill(name)
    mismatches = verify(skill, directory)
    if mismatches:
        raise HashMismatch(mismatches)
    return directory


# 下载与更新


def repo_slug(source: str) -> str:
    """来源仓库地址中的「所有者/仓库」。"""
    for prefix in GITHUB_PREFIXES:
        if source.startswith(prefix):
            return source[len(prefix):].removesuffix(".git").strip("/")
    raise VendorError(f"只支持 GitHub 仓库地址：{source}")


def archive_url(source: str, commit: str) -> str:
    return ARCHIVE_URL.format(slug=repo_slug(source), commit=commit)


def extract(archive: bytes, name: str, path: str | None, dest: Path) -> str:
    """从源码归档(顶层为一个目录)中只解出 skill 目录的普通文件到 dest，返回该目录在来源仓库中的路径。

    path 为空时按 SKILL.md 头信息的 name 找唯一的目录。路径的每一段都经 segment 校验，防止 `..` 解到目录之外；
    先解到同级的 `.partial`，完成后再替换 dest，中途失败不留半个目录。
    """
    try:
        tar = tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz")  # noqa: SIM115 打开失败要单独归类，下面立即 with
    except (tarfile.TarError, OSError) as error:
        raise VendorError(f"源码归档无法读取：{error}") from error
    with tar:
        entries = {PurePosixPath(*PurePosixPath(member.name).parts[1:]): member
                   for member in tar.getmembers() if member.isfile() and len(PurePosixPath(member.name).parts) > 1}
        if path is None:
            found = sorted({str(relative.parent) for relative, member in entries.items()
                            if relative.name == SKILL_FILE and _skill_name(tar, member) == name})
            if len(found) != 1:
                raise VendorError(f"源码归档中 name 为 {name} 的 skill 目录有 {len(found)} 个：{found}")
            path = found[0]
        root = PurePosixPath(path)
        selected = {relative.relative_to(root): member for relative, member in entries.items()
                    if relative.is_relative_to(root)}
        if PurePosixPath(SKILL_FILE) not in selected:
            raise VendorError(f"源码归档的 {path} 下没有 {SKILL_FILE}")
        partial = dest.with_name(dest.name + PARTIAL_SUFFIX)
        shutil.rmtree(partial, ignore_errors=True)
        try:
            for relative, member in selected.items():
                target = partial.joinpath(*(segment(part) for part in relative.parts))
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = tar.extractfile(member)
                target.write_bytes(handle.read() if handle else b"")
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
    shutil.rmtree(dest, ignore_errors=True)
    partial.rename(dest)
    return path


def ensure_present(skill: LockedSkill, fetch: Fetch, directory: Path) -> Path:
    """已锁定 skill 的副本：缺失时按锁定的 commit 下载并解出；与清单不符时抛出 HashMismatch。

    已有副本不符时不重新下载覆盖(可能是有人改过，要人来看)；刚下载的不符则删除，不留下未经核对的内容。
    """
    if skill.commit is None:
        raise VendorError(f"{skill.name} 尚未锁定")
    downloaded = not directory.is_dir()
    if downloaded:
        directory.parent.mkdir(parents=True, exist_ok=True)
        extract(fetch(skill.source, skill.commit), skill.name, skill.path, directory)
    mismatches = verify(skill, directory)
    if mismatches:
        if downloaded:
            shutil.rmtree(directory)
        raise HashMismatch(mismatches)
    return directory


def update(skill: LockedSkill, commit: str, fetch: Fetch, directory: Path, *, license: str,
           today: str) -> tuple[LockedSkill, LockChange]:
    """把一个 skill 换到另一个完整 commit：新内容先解到 `.partial` 旁的临时目录，记录逐文件哈希后才替换副本。

    现有副本与旧清单不符时停止(不覆盖被改过的副本)。返回新的条目与文件变化；不写锁定清单，由调用方确认后写。
    """
    if not COMMIT.match(commit):
        raise VendorError(f"commit 须为 40 位小写十六进制：{commit}")
    if skill.locked and directory.is_dir():
        mismatches = verify(skill, directory)
        if mismatches:
            raise HashMismatch(mismatches)
    staging = directory.with_name(directory.name + ".new")
    try:
        path = extract(fetch(skill.source, commit), skill.name, skill.path, staging)
        files = file_hashes(staging)
        shutil.rmtree(directory, ignore_errors=True)
        staging.rename(directory)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    locked = LockedSkill(skill.name, skill.source, commit, path, license, tree_hash(files), files, today)
    return locked, _change(skill, locked)


# 内部


def _lock_problems(data: Any) -> list[str]:
    if not isinstance(data, dict) or data.get("lockVersion") != LOCK_VERSION:
        return [f"lockVersion 须为 {LOCK_VERSION}"]
    skills = data.get("skills")
    if not isinstance(skills, list):
        return ["skills 须为列表"]
    problems: list[str] = []
    names: set[str] = set()
    for index, item in enumerate(skills):
        key = f"skills[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{key} 须为对象")
            continue
        problems += [f"{key}.{name}：不认识的键" for name in item if name not in SKILL_KEYS]
        name = item.get("name")
        if not isinstance(name, str) or not NAME.match(name) or name in names:
            problems.append(f"{key}.name：须为小写字母、数字与连字符，且不重复")
        names.add(str(name))
        if not isinstance(item.get("source"), str):
            problems.append(f"{key}.source：缺少必填项")
        if "commit" in item:
            if not isinstance(item["commit"], str) or not COMMIT.match(item["commit"]):
                problems.append(f"{key}.commit：须为 40 位小写十六进制 commit")
            problems += [f"{key}.{name}：已锁定的条目缺少必填项" for name in LOCKED_KEYS if name not in item]
            if "files" in item and not isinstance(item["files"], dict):
                problems.append(f"{key}.files：须为「相对路径 → sha256」的对象")
    return problems


def _skill(item: dict[str, Any]) -> LockedSkill:
    return LockedSkill(item["name"], item["source"], item.get("commit"), item.get("path"), item.get("license"),
                       item.get("treeHash"), dict(item.get("files") or {}), item.get("lockedAt"))


def _skill_name(tar: tarfile.TarFile, member: tarfile.TarInfo) -> str | None:
    handle = tar.extractfile(member)
    text = handle.read().decode("utf-8", errors="replace") if handle else ""
    if not text.startswith("---\n"):
        return None
    header, _, _ = text[4:].partition("\n---")
    try:
        data = yaml.safe_load(header)
    except yaml.YAMLError:
        return None
    return data.get("name") if isinstance(data, dict) else None


def _change(old: LockedSkill, new: LockedSkill) -> LockChange:
    before, after = old.files, new.files
    return LockChange(new.name, old.commit, new.commit or "", tuple(sorted(set(after) - set(before))),
                      tuple(sorted(set(before) - set(after))),
                      tuple(sorted(path for path in set(before) & set(after) if before[path] != after[path])))
