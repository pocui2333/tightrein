"""版本快照与防作弊检查(architecture/03 2.6.2，design 14.8、14.9)。

| VersionSpec | 构建方式 |
|---|---|
| commit | 只读的 `git archive <commit>` 写到临时 tar 文件，解包到 versions/<标签>/ |
| commit + patch | 同上，再在快照目录中 `git apply`；先 `--check`，不能干净应用时报错 |
| use_worktree | 复制 tightrein 仓库中已跟踪与未被忽略的文件(`git ls-files -co --exclude-standard`) |

快照目录位于工作区的 data/ 下(本工具仓库的子目录)。git apply 在仓库的子目录中运行时会把补丁路径当作相对仓库根目录
并静默跳过目录之外的文件，因此以 GIT_CEILING_DIRECTORIES 截断仓库查找，让补丁只作用于快照目录。

之后：
1. 防作弊：候选快照相对基线快照改动的文件触及 evaluation、guards、improve 模块、improve 的 skill 或任一工作区的
   evals/ 时拒绝评测；
2. 数据库副本：用 SQLite 在线备份把 data/tightrein.db 复制一次，再复制到每个版本快照工作区的 data/ 下，所有版本读到
   相同的知识索引与历史数据，真实数据库不会被快照写入；
3. 被测项目代码：对用例涉及的每个 commit 以 git archive 导出到 snapshots/<commit>/，设为只读。
已存在的快照目录直接复用(续跑)。失败抛出 SnapshotFailed，已生成的目录保留便于排查。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import tarfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from fnmatch import fnmatch
from pathlib import Path

from tightrein.evaluation.errors import EvaluationRefused, SnapshotFailed
from tightrein.evaluation.variants import VersionSpec
from tightrein.store.files.layout import ToolLayout
from tightrein.vcs.errors import VcsError
from tightrein.vcs.process import VcsProcess

FORBIDDEN_PATTERNS = (
    "core/tightrein/evaluation/*",
    "core/tightrein/guards/*",
    "core/tightrein/pipeline/improve/*",
    "skills/improve/*",
    "workspaces/*/evals/*",
)
CEILING = "GIT_CEILING_DIRECTORIES"
READ_ONLY_FILE = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
READ_ONLY_DIR = READ_ONLY_FILE | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
SKIPPED = frozenset({".git"})


def _archive(process: VcsProcess, repo: Path, commit: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive = destination.parent / f".{destination.name}.tar"
    try:
        process.git(repo, "archive", "--format=tar", "-o", str(archive), commit)
        destination.mkdir()
        with tarfile.open(archive) as bundle:
            bundle.extractall(destination, filter="data")
    except (VcsError, tarfile.TarError, OSError) as error:
        raise SnapshotFailed(f"无法导出 {repo} 的 {commit} 到 {destination}：{error}") from error
    finally:
        archive.unlink(missing_ok=True)


def _apply(process: VcsProcess, destination: Path, patch: Path) -> None:
    outside = replace(process, environ={**process.environ, CEILING: str(destination.parent)})
    try:
        outside.git(destination, "apply", "--check", "--whitespace=nowarn", str(patch.resolve()))
        outside.git(destination, "apply", "--whitespace=nowarn", str(patch.resolve()))
    except VcsError as error:
        raise SnapshotFailed(f"{patch} 不能干净地应用到 {destination}：{error}") from error


def _copy_worktree(process: VcsProcess, repo: Path, destination: Path) -> None:
    try:
        listed = process.git(repo, "ls-files", "-co", "--exclude-standard", "-z").stdout
    except VcsError as error:
        raise SnapshotFailed(f"无法列出 {repo} 的文件：{error}") from error
    destination.mkdir(parents=True)
    for relative in filter(None, listed.split("\0")):
        source = repo / relative
        if source.is_file() and not source.is_symlink():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def build_snapshot(spec: VersionSpec, tool_root: Path, destination: Path, process: VcsProcess) -> Path:
    if destination.exists():
        return destination
    if spec.use_worktree:
        _copy_worktree(process, tool_root, destination)
        return destination
    _archive(process, tool_root, spec.commit, destination)
    if spec.patch is not None:
        _apply(process, destination, spec.patch)
    return destination


def _files(root: Path) -> dict[str, bytes]:
    found = {}
    for directory, directories, names in os.walk(root):
        directories[:] = [name for name in directories if name not in SKIPPED]
        for name in names:
            path = Path(directory) / name
            found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


def changed_files(baseline: Path, candidate: Path) -> list[str]:
    before, after = _files(baseline), _files(candidate)
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def forbidden(paths: Iterable[str]) -> list[str]:
    return sorted(path for path in paths if any(fnmatch(path, pattern) for pattern in FORBIDDEN_PATTERNS))


def check_candidates(baseline: Path, candidates: Sequence[Path]) -> None:
    """候选快照改动了防作弊路径时抛出 EvaluationRefused。"""
    touched = sorted({path for candidate in candidates for path in forbidden(changed_files(baseline, candidate))})
    if touched:
        raise EvaluationRefused(touched)


def copy_database(source: Path, targets: Sequence[Path]) -> None:
    """SQLite 在线备份：先备份到第一个目标，再逐个复制；源库不存在时报错。"""
    if not source.is_file():
        raise SnapshotFailed(f"数据库 {source} 不存在")
    if not targets:
        return
    first = targets[0]
    first.parent.mkdir(parents=True, exist_ok=True)
    origin = sqlite3.connect(source)
    copy = sqlite3.connect(first)
    try:
        origin.backup(copy)
    except sqlite3.Error as error:
        raise SnapshotFailed(f"数据库 {source} 备份失败：{error}") from error
    finally:
        origin.close()
        copy.close()
    for target in targets[1:]:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(first, target)


def workspace_database(snapshot: Path, project: str) -> Path:
    return ToolLayout(snapshot).workspace(project).database()


def export_project(process: VcsProcess, repo: Path, commit: str, destination: Path) -> Path:
    """被测项目在 commit 的只读快照；已存在时复用。"""
    if destination.exists():
        return destination
    _archive(process, repo, commit, destination)
    for directory, directories, names in os.walk(destination, topdown=False):
        for name in names:
            os.chmod(Path(directory) / name, READ_ONLY_FILE)
        os.chmod(directory, READ_ONLY_DIR)
    return destination


def build_versions(versions: Mapping[str, VersionSpec], tool_root: Path, directories: Mapping[str, Path],
                   process: VcsProcess, database: Path, project: str) -> dict[str, Path]:
    """构建全部版本快照，第一个为基线；检查防作弊路径后复制数据库。"""
    built = {label: build_snapshot(spec, tool_root, directories[label], process) for label, spec in versions.items()}
    labels = list(built)
    check_candidates(built[labels[0]], [built[label] for label in labels[1:]])
    pending = [workspace_database(path, project) for path in built.values()]
    copy_database(database, [target for target in pending if not target.exists()])
    return built
