"""改动的规则检查：受保护文件、测试改动、跳过标记、针对复现输入写死与改动量上限(architecture/02 3.8、3.9，
design 5.5、12.4)。运行后检查(after)与修复分支的 diff 检查(check_diff)共用这里的规则。

- 受保护：路径匹配 protectedPaths，或新增、删除的行含 protectedPatterns；路径在 approvedProtectedPaths 中的不算。
- 测试：路径匹配 testPaths。
- 跳过标记：新增的行含 skipMarkers。
- 写死：新增行中的字符串与数字字面量，与该 Issue 复现检查目录中文件里的字面量完全相同的逐条列出(suspected-hardcode，
  只提示不判不通过)；长度不足 4 个字符的字面量不比较，避免 0、1、"id" 这类常见值误报。
- 改动量：不计匹配 testPaths 的文件，改动文件数超过 maxFiles，或增删行数之和超过 maxLines。
- 残留：新增的行匹配 residuePatterns(正则，调试输出、注释掉的废弃代码等)。
- 计划外：改动的文件不在计划的文件清单中(含未跟踪的新文件，即临时文件)。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from tightrein.domain.enums import ViolationKind
from tightrein.guards.file_state import walk
from tightrein.guards.policy import GuardSettings
from tightrein.guards.protected import matching_markers, matching_pattern
from tightrein.guards.report import Violation
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.vcs.git_read import GitReader

LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'|(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])')


@dataclass(frozen=True)
class ChangedFile:
    path: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    lines_added: int = 0
    lines_removed: int = 0


def literals(text: str, min_length: int) -> set[str]:
    """不短于 min_length(runtime.guards.minLiteralLength)的字符串与数字字面量。"""
    found = set()
    for match in LITERAL.finditer(text):
        value = next((group for group in match.groups() if group is not None), "")
        if len(value) >= min_length:
            found.add(value)
    return found


def reproduction_literals(layout: WorkspaceLayout, issue_id: str, min_length: int) -> set[str]:
    """该 Issue 复现检查目录中除 check.yaml 以外的文件(请求参数与期望值所在)里的字面量。"""
    directory = layout.regression_dir(issue_id)
    checklist = layout.regression_checklist(issue_id).name
    found: set[str] = set()
    for relative in walk(directory):
        path = directory / relative
        if relative != checklist and path.is_file():
            found |= literals(path.read_text(encoding="utf-8", errors="replace"), min_length)
    return found


def collect_changes(git: GitReader, repo: Path, base: str, paths: Sequence[str] | None = None) -> list[ChangedFile]:
    """base 到工作目录的改动(已跟踪文件的 diff)加未跟踪的新文件(全部行记为新增)。paths 给出时只看这些文件。"""
    if paths is not None and not paths:
        return []
    diff = git.diff(repo, base, paths=paths)
    changes = [
        ChangedFile(item.path, item.added_lines, item.removed_lines, item.added or 0, item.removed or 0)
        for item in diff.files
    ]
    wanted = None if paths is None else set(paths)
    for path in git.untracked(repo):
        if wanted is not None and path not in wanted:
            continue
        file_path = repo / path
        if file_path.is_symlink() or not file_path.is_file():
            continue
        lines = tuple(file_path.read_text(encoding="utf-8", errors="replace").splitlines())
        changes.append(ChangedFile(path, lines, (), len(lines), 0))
    return sorted(changes, key=lambda item: item.path)


def protected_violations(changes: Iterable[ChangedFile], settings: GuardSettings,
                         approved: Sequence[str]) -> list[Violation]:
    violations = []
    for change in changes:
        if matching_pattern(change.path, approved) is not None:
            continue
        pattern = matching_pattern(change.path, settings.protected_paths)
        if pattern is not None:
            violations.append(Violation(ViolationKind.PROTECTED_MODIFIED, change.path, f"匹配受保护路径 {pattern}"))
            continue
        for action, lines in (("新增", change.added), ("删除", change.removed)):
            found = sorted({marker for line in lines for marker in matching_markers(line, settings.protected_patterns)})
            for marker in found:
                violations.append(Violation(ViolationKind.PROTECTED_MODIFIED, change.path, f"{action}了 {marker}"))
    return violations


def modified_test_violations(changes: Iterable[ChangedFile], settings: GuardSettings) -> list[Violation]:
    violations = []
    for change in changes:
        pattern = matching_pattern(change.path, settings.test_paths)
        if pattern is not None:
            violations.append(Violation(ViolationKind.TEST_MODIFIED, change.path, f"匹配测试路径 {pattern}"))
    return violations


def skip_violations(changes: Iterable[ChangedFile], settings: GuardSettings) -> list[Violation]:
    violations = []
    for change in changes:
        found = sorted({marker for line in change.added for marker in matching_markers(line, settings.skip_markers)})
        for marker in found:
            violations.append(Violation(ViolationKind.SKIP_MARKER_ADDED, change.path, f"新增了 {marker}"))
    return violations


def hardcode_violations(changes: Iterable[ChangedFile], reproduction: set[str], min_length: int) -> list[Violation]:
    violations = []
    for change in changes:
        same = sorted({value for line in change.added for value in literals(line, min_length)} & reproduction)
        for value in same:
            violations.append(Violation(ViolationKind.SUSPECTED_HARDCODE, change.path,
                                        f"新增的字面量 {value} 与复现检查中的输入或期望值相同"))
    return violations


def size_violations(changes: Sequence[ChangedFile], settings: GuardSettings) -> list[Violation]:
    counted = [change for change in changes if matching_pattern(change.path, settings.test_paths) is None]
    files = len(counted)
    lines = sum(change.lines_added + change.lines_removed for change in counted)
    violations = []
    if files > settings.max_files:
        violations.append(Violation(ViolationKind.SIZE_EXCEEDED, None,
                                    f"改动了 {files} 个文件(不含测试)，超过上限 {settings.max_files}"))
    if lines > settings.max_lines:
        violations.append(Violation(ViolationKind.SIZE_EXCEEDED, None,
                                    f"增删 {lines} 行(不含测试)，超过上限 {settings.max_lines}"))
    return violations


def residue_violations(changes: Iterable[ChangedFile], settings: GuardSettings) -> list[Violation]:
    patterns = [re.compile(pattern) for pattern in settings.residue_patterns]
    violations = []
    for change in changes:
        for line in change.added:
            found = next((pattern.pattern for pattern in patterns if pattern.search(line)), None)
            if found is not None:
                violations.append(Violation(ViolationKind.RESIDUE_ADDED, change.path,
                                            f"新增行匹配残留模式 {found}：{line.strip()}"))
    return violations


def outside_plan_violations(changes: Iterable[ChangedFile], plan_files: Iterable[str]) -> list[Violation]:
    planned = set(plan_files)
    return [Violation(ViolationKind.OUTSIDE_PLAN, change.path, "不在计划的文件清单中")
            for change in changes if change.path not in planned]
