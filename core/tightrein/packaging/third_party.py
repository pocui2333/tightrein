"""第三方 skill 的锁定与校验(architecture/09 第 7 节，design 9.11)。

- 锁定清单 third_party/skills.lock.yaml：每个条目的来源、完整 commit、在来源仓库中的路径、许可证、目录哈希、
  逐文件哈希、锁定日期与 9.11 的核实数据(核实日期、星标数、最近提交日期、是否归档)。只写了 name 与 source 的条目
  尚未锁定，由 `third-party lock` 补全；
- 目录哈希：全部文件(不含 .git)按相对路径排序，逐行拼接「相对路径、制表符、内容的 sha256、换行」后求 sha256，
  不计权限与修改时间；
- 下载：按 commit 取来源仓库的源码归档，只解出该 skill 的目录，放到本工具仓库的缓存 local/third_party-cache/<名称>/<commit>/；
  不执行任何 git 命令。下载与来源仓库的查询都由调用方注入；下载遇到网络类错误时换另一条路(直连与经代理互换)重试一次；
- 校验：任一文件哈希不符即停止，报出文件与期望、实际哈希；已有缓存不自动重新下载覆盖。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tarfile
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import tzinfo
from pathlib import Path, PurePosixPath
from typing import Any

from tightrein.config import network
from tightrein.config.network import Reroute
from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock, local_date, parse_iso
from tightrein.packaging import skills_check
from tightrein.pipeline.learn.steps.third_party import RepoFacts, RepoQuery, judge
from tightrein.sources.common.http import HttpRequest, Transport
from tightrein.store.files import yaml_text
from tightrein.store.files.layout import segment
from tightrein.vcs.errors import VcsError
from tightrein.vcs.process import VcsProcess

LOCK_VERSION = 1
HASH_PREFIX = "sha256:"
GITHUB_PREFIXES = ("https://github.com/", "git@github.com:")
ARCHIVE_URL = "https://codeload.github.com/{slug}/tar.gz/{commit}"
COMMIT = re.compile(r"^[0-9a-f]{40}$")
SKIP_DIRECTORY = ".git"
PARTIAL_SUFFIX = ".partial"
SKILL_KEYS = ("name", "source", "ref", "path", "license", "treeHash", "files", "lockedAt", "verification")
LOCKED_KEYS = ("ref", "path", "treeHash", "files", "lockedAt", "verification")

Fetch = Callable[[str, str], bytes]
"""(来源仓库, commit) → 源码归档(tar.gz)的内容。"""
CachePath = Callable[[str, str], Path]
"""(名称, commit) → 缓存目录。"""


class ThirdPartyError(Exception):
    """锁定、下载或校验失败。"""


class LockError(ThirdPartyError, ValueError):
    """锁定清单不合格(退出码 2)。"""


class ThresholdError(ThirdPartyError):
    """来源仓库不满足 design 9.11 的采用门槛。"""


class HashMismatch(ThirdPartyError):
    def __init__(self, issues: Sequence[VerifyIssue]) -> None:
        self.issues = list(issues)
        super().__init__("；".join(issue.describe() for issue in issues))


@dataclass(frozen=True)
class LockedFile:
    path: str
    sha256: str


@dataclass(frozen=True)
class Verification:
    verified_at: str
    stars: int
    last_commit: str
    archived: bool

    def to_dict(self) -> dict[str, Any]:
        return {"verifiedAt": self.verified_at, "stars": self.stars, "lastCommit": self.last_commit,
                "archived": self.archived}


@dataclass(frozen=True)
class LockedSkill:
    name: str
    source: str
    ref: str | None = None
    path: str | None = None
    license: str | None = None
    tree_hash: str | None = None
    files: tuple[LockedFile, ...] = ()
    locked_at: str | None = None
    verification: Verification | None = None

    @property
    def locked(self) -> bool:
        return self.ref is not None

    def to_dict(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            "name": self.name, "source": self.source, "ref": self.ref, "path": self.path, "license": self.license,
            "treeHash": self.tree_hash,
            "files": [{"path": item.path, "sha256": item.sha256} for item in self.files] if self.locked else None,
            "lockedAt": self.locked_at,
            "verification": self.verification.to_dict() if self.verification else None,
        }
        return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True)
class LockChange:
    name: str
    old_ref: str | None
    new_ref: str
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]

    @property
    def replaces(self) -> bool:
        """改写已锁定的条目(需要用户确认)。"""
        return self.old_ref is not None

    def describe(self) -> str:
        head = f"{self.name}：{self.old_ref or '未锁定'} → {self.new_ref}"
        parts = [f"{label} {', '.join(items)}" for label, items in
                 (("新增", self.added), ("删除", self.removed), ("改动", self.changed)) if items]
        return head + (f"(文件：{'；'.join(parts)})" if parts and self.replaces else "")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "oldRef": self.old_ref, "newRef": self.new_ref, "added": list(self.added),
                "removed": list(self.removed), "changed": list(self.changed)}


@dataclass(frozen=True)
class VerifyIssue:
    name: str
    path: str
    expected: str | None
    actual: str | None

    def describe(self) -> str:
        return f"{self.name} 的 {self.path}：期望 {self.expected or '不存在'}，实际 {self.actual or '不存在'}"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": self.path, "expected": self.expected, "actual": self.actual}


# 锁定清单


def _problems(data: Any) -> list[str]:
    if not isinstance(data, Mapping) or data.get("lockVersion") != LOCK_VERSION:
        return [f"lockVersion 须为 {LOCK_VERSION}"]
    skills = data.get("skills")
    if not isinstance(skills, list):
        return ["skills 须为列表"]
    problems, names = [], set()
    for index, item in enumerate(skills):
        key = f"skills[{index}]"
        if not isinstance(item, Mapping):
            problems.append(f"{key} 须为映射")
            continue
        problems += [f"{key}.{name}: 不认识的键" for name in item if name not in SKILL_KEYS]
        name = item.get("name")
        if not isinstance(name, str) or not skills_check.NAME.match(name) or name in names:
            problems.append(f"{key}.name: 须为小写字母、数字与连字符，且不重复")
        names.add(name)
        if not isinstance(item.get("source"), str):
            problems.append(f"{key}.source: 缺少必填项")
        if "ref" in item:
            if not isinstance(item["ref"], str) or not COMMIT.match(item["ref"]):
                problems.append(f"{key}.ref: 须为 40 位小写十六进制 commit")
            problems += [f"{key}.{field}: 已锁定的条目缺少必填项" for field in LOCKED_KEYS if field not in item]
    return problems


def _skill(item: Mapping[str, Any]) -> LockedSkill:
    verification = item.get("verification")
    return LockedSkill(
        item["name"], item["source"], item.get("ref"), item.get("path"), item.get("license"), item.get("treeHash"),
        tuple(LockedFile(entry["path"], entry["sha256"]) for entry in item.get("files", ())), item.get("lockedAt"),
        Verification(verification["verifiedAt"], int(verification["stars"]), verification["lastCommit"],
                     bool(verification["archived"])) if verification else None,
    )


def read_lock(path: Path) -> list[LockedSkill]:
    """读取锁定清单；文件不存在时为空。"""
    if not path.is_file():
        return []
    try:
        data = yaml_text.load(path.read_text(encoding="utf-8"))
    except yaml_text.YamlError as error:
        raise LockError(f"{path}：{error}") from error
    problems = _problems(data)
    if problems:
        raise LockError(f"{path}：{'；'.join(problems)}")
    try:
        return [_skill(item) for item in data["skills"]]
    except (KeyError, TypeError, ValueError) as error:
        raise LockError(f"{path}：条目的字段不合格：{error}") from error


def write_lock(path: Path, skills: Sequence[LockedSkill]) -> None:
    text = yaml_text.dump({"lockVersion": LOCK_VERSION, "skills": [skill.to_dict() for skill in skills]})
    path.write_text(text, encoding="utf-8")


# 哈希


def file_hashes(directory: Path) -> tuple[LockedFile, ...]:
    """目录下全部文件(跟随链接，不含 .git)的相对路径与 sha256，按相对路径排序。"""
    found = []
    for folder, directories, names in os.walk(directory, followlinks=True):
        directories[:] = [name for name in directories if name != SKIP_DIRECTORY]
        for name in names:
            path = Path(folder) / name
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            found.append(LockedFile(path.relative_to(directory).as_posix(), digest))
    return tuple(sorted(found, key=lambda item: item.path))


def tree_hash(files: Sequence[LockedFile]) -> str:
    text = "".join(f"{item.path}\t{item.sha256}\n" for item in sorted(files, key=lambda item: item.path))
    return HASH_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify(skill: LockedSkill, directory: Path) -> list[VerifyIssue]:
    """按清单逐文件核对目录；目录不存在时报一项。"""
    if not directory.is_dir():
        return [VerifyIssue(skill.name, str(directory), skill.tree_hash, None)]
    expected = {item.path: item.sha256 for item in skill.files}
    actual = {item.path: item.sha256 for item in file_hashes(directory)}
    return [VerifyIssue(skill.name, path, expected.get(path), actual.get(path))
            for path in sorted(set(expected) | set(actual)) if expected.get(path) != actual.get(path)]


# 来源仓库


def repo_slug(source: str) -> str:
    """来源仓库地址中的「所有者/仓库」。"""
    for prefix in GITHUB_PREFIXES:
        if source.startswith(prefix):
            return source[len(prefix):].removesuffix(".git").strip("/")
    raise ValueError(f"只支持 GitHub 仓库地址：{source}")


def _gh_json(process: VcsProcess, cwd: Path, source: str, endpoint: str) -> Any:
    completed = process.gh(cwd, "api", f"repos/{repo_slug(source)}{endpoint}")
    return json.loads(completed.stdout)


def repo_facts(process: VcsProcess, cwd: Path, source: str) -> RepoFacts:
    """`gh api repos/<所有者>/<仓库>` 的星标数、最近推送日期、是否归档与许可证；失败时抛出 LookupError。"""
    try:
        data = _gh_json(process, cwd, source, "")
        return RepoFacts(int(data["stargazers_count"]), parse_iso(data["pushed_at"]).date(), bool(data["archived"]),
                         (data.get("license") or {}).get("spdx_id"))
    except (VcsError, ValueError, KeyError, TypeError) as error:
        raise LookupError(f"查询 {source} 失败：{type(error).__name__}: {error}") from error


def head_commit(process: VcsProcess, cwd: Path, source: str) -> str:
    """来源仓库默认分支的最新 commit(`gh api repos/<所有者>/<仓库>/commits/HEAD`)。"""
    try:
        sha = _gh_json(process, cwd, source, "/commits/HEAD")["sha"]
    except (VcsError, ValueError, KeyError, TypeError) as error:
        raise LookupError(f"查询 {source} 的最新 commit 失败：{type(error).__name__}: {error}") from error
    if not isinstance(sha, str) or not COMMIT.match(sha):
        raise LookupError(f"{source} 返回的 commit 不合格：{sha}")
    return sha


@dataclass(frozen=True)
class AlternateRoute:
    """下载遇到网络类错误时的另一条路(config.network.rerouted 的环境建立的 Transport)与换路的记录方式。"""

    transport: Transport
    before: str
    after: str
    patterns: tuple[str, ...]
    record: Callable[[Reroute], None]


def fetcher(transport: Transport, timeout_seconds: float, alternate: AlternateRoute | None = None) -> Fetch:
    """没有得到响应且原因属于网络类错误时，经 alternate 换路重试一次并记下。"""

    def fetch(source: str, commit: str) -> bytes:
        url = ARCHIVE_URL.format(slug=repo_slug(source), commit=commit)
        request = HttpRequest("GET", url, timeout_seconds=timeout_seconds)
        response = transport(request)
        if (alternate is not None and response.status is None
                and network.is_network_failure(response.error or "", alternate.patterns)):
            retried = alternate.transport(request)
            alternate.record(Reroute(url, alternate.before, alternate.after, response.error or "", retried.ok))
            response = retried
        if not response.ok:
            raise ThirdPartyError(f"下载 {url} 失败：{response.status or response.error}")
        return response.body

    return fetch


# 归档


def _skill_name(tar: tarfile.TarFile, member: tarfile.TarInfo) -> str | None:
    handle = tar.extractfile(member)
    header, _ = skills_check.split(handle.read().decode("utf-8", errors="replace")) if handle else (None, "")
    try:
        data = yaml_text.load(header) if header else None
    except yaml_text.YamlError:
        return None
    return data.get("name") if isinstance(data, Mapping) else None


def extract(archive: bytes, name: str, path: str | None, dest: Path) -> str:
    """从源码归档(顶层为一个目录)中只解出 skill 目录的普通文件到 dest，返回该目录在仓库中的路径。

    path 为空时按 SKILL.md 的 name 找唯一的目录。先解到同级的临时目录，完成后替换 dest。
    """
    try:
        tar = tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz")
    except (tarfile.TarError, OSError) as error:
        raise ThirdPartyError(f"源码归档无法读取：{error}") from error
    with tar:
        entries = {PurePosixPath(*PurePosixPath(member.name).parts[1:]): member
                   for member in tar.getmembers() if member.isfile() and len(PurePosixPath(member.name).parts) > 1}
        if path is None:
            found = sorted({str(relative.parent) for relative, member in entries.items()
                            if relative.name == skills_check.SKILL_FILE and _skill_name(tar, member) == name})
            if len(found) != 1:
                raise ThirdPartyError(f"源码归档中 name 为 {name} 的 skill 目录有 {len(found)} 个：{found}")
            path = found[0]
        root = PurePosixPath(path)
        selected = {relative.relative_to(root): member for relative, member in entries.items()
                    if relative.is_relative_to(root)}
        if skills_check.SKILL_FILE not in {str(relative) for relative in selected}:
            raise ThirdPartyError(f"源码归档的 {path} 下没有 {skills_check.SKILL_FILE}")
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


def ensure_cached(skill: LockedSkill, fetch: Fetch, cache: CachePath) -> Path:
    """已锁定 skill 的缓存目录：缺失时下载并解出；与清单不符时抛出 HashMismatch(刚下载的目录随之删除)。"""
    if skill.ref is None:
        raise LockError(f"{skill.name} 尚未锁定，先执行 tightrein admin third-party lock")
    directory = cache(skill.name, skill.ref)
    downloaded = not directory.is_dir()
    if downloaded:
        directory.parent.mkdir(parents=True, exist_ok=True)
        extract(fetch(skill.source, skill.ref), skill.name, skill.path, directory)
    issues = verify(skill, directory)
    if issues:
        if downloaded:
            shutil.rmtree(directory)
        raise HashMismatch(issues)
    return directory


# 锁定


def _change(old: LockedSkill, new: LockedSkill) -> LockChange:
    before = {item.path: item.sha256 for item in old.files}
    after = {item.path: item.sha256 for item in new.files}
    return LockChange(new.name, old.ref, new.ref or "", tuple(sorted(set(after) - set(before))),
                      tuple(sorted(set(before) - set(after))),
                      tuple(sorted(path for path in set(before) & set(after) if before[path] != after[path])))


def lock(skills: Sequence[LockedSkill], selected: Collection[str], *, ref: str | None, head: Callable[[str], str],
         facts: RepoQuery, fetch: Fetch, cache: CachePath, config: ProjectConfig, clock: Clock,
         zone: tzinfo | None = None) -> tuple[list[LockedSkill], list[LockChange]]:
    """锁定 selected 中的条目(为空时全部)：取 commit(ref 或来源仓库的最新 commit)，核实 9.11 的门槛，下载到缓存并记录
    逐文件哈希。commit 与已锁定的相同时不变。返回新的清单与变化；不写文件。"""
    unknown = set(selected) - {skill.name for skill in skills}
    if unknown:
        raise LockError(f"锁定清单中没有：{', '.join(sorted(unknown))}")
    if ref is not None and (len(selected) != 1 or not COMMIT.match(ref)):
        raise LockError("--ref 须为 40 位小写十六进制 commit，且只能与一个名称同用")
    today = local_date(clock.now(), zone)
    result, changes = [], []
    for skill in skills:
        if selected and skill.name not in selected:
            result.append(skill)
            continue
        commit = ref or head(skill.source)
        if skill.ref == commit:
            result.append(skill)
            continue
        found = facts(skill.source)
        verdict = judge(skill.name, skill.source, found, config, today)
        if not verdict.passed:
            raise ThresholdError(f"{skill.name} 不满足采用门槛：{verdict.detail}")
        directory = cache(skill.name, commit)
        directory.parent.mkdir(parents=True, exist_ok=True)
        path = extract(fetch(skill.source, commit), skill.name, skill.path, directory)
        files = file_hashes(directory)
        locked = replace(skill, ref=commit, path=path, license=found.license, tree_hash=tree_hash(files), files=files,
                         locked_at=today.isoformat(),
                         verification=Verification(today.isoformat(), found.stars, found.last_commit.isoformat(),
                                                   found.archived))
        result.append(locked)
        changes.append(_change(skill, locked))
    return result, changes
