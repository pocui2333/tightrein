"""改动量与规则检查(程序判定，不调用模型)，每一项不通过时给出性质。

| 检查 | 性质 |
|---|---|
| 改动落在 worktree 之外、碰了禁改文件(protocol.boundaries.check_round) | 需要用户 |
| 增删的行含受保护标记(protectedMarkers，如许可证头、生成代码标记)，方案中已确认改动的文件(protectedTouches)除外 | 需要用户 |
| 改动量超上限(不算测试、锁文件与生成文件；未跟踪的新文件全部行计为新增) | 第一次为局部问题：按方案收敛；同一次实施中再超出为需要用户 |
| 方案外的已跟踪文件 | 方案缺口 |
| 方案外的未跟踪文件(新写的测试除外) | 局部问题：临时文件，交付前删除 |
| 改了方案没列出的已有测试 | 局部问题：不能为了让测试通过去改测试 |
| 新增行含跳过标记、匹配残留模式(调试输出等) | 局部问题 |

疑似写死只提示不判失败：非测试文件新增行中长度不少于 minLiteralLength 的字面量，与本次写的测试或 Issue 中的
输入相同的逐条列出，交给审查读代码判断是不是特判。
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tightrein.implement.check.changes import UNTRACKED, Changes
from tightrein.implement.check.findings import LOCAL, NEEDS_USER, PLAN_GAP, Finding
from tightrein.protocol.boundaries import OVER_CAP, check_round, matching_pattern
from tightrein.settings.load import Settings

CHECK = "rules"
SIZE_GUIDANCE = "按方案收敛：撤回方案外与非必要的改动，不得以放宽上限处理"
SIZE_REPEATED = "收敛后仍超出改动量上限，交给用户决定"
LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'|(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])')
DELETED = "D"


@dataclass(frozen=True)
class RuleSettings:
    skip_markers: tuple[str, ...]
    residue: tuple[re.Pattern[str], ...]
    min_literal_length: int
    protected_markers: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, settings: Settings) -> RuleSettings:
        section = settings.section("implement.check")
        return cls(tuple(section["skipMarkers"]), tuple(re.compile(item) for item in section["residuePatterns"]),
                   int(section["minLiteralLength"]), tuple(section["protectedMarkers"]))


@dataclass(frozen=True)
class HardcodeHint:
    path: str
    literal: str

    def text(self) -> str:
        return f"{self.path}：新增的字面量 {self.literal!r} 与本次的测试或 Issue 中的输入相同"


def evaluate(changes: Changes, *, planned: Collection[str], worktree: Path, settings: Settings,
             rules: RuleSettings, over_cap_before: bool, approved: Collection[str] = ()) -> list[Finding]:
    """planned 为方案列出的文件；为空(没有方案)时不做方案外的检查。approved 为方案中已确认要改的受保护文件。"""
    project = settings.project
    tests = project.test_patterns if project is not None else ()
    findings = [_boundary(violation.kind, violation.path, violation.detail, over_cap_before)
                for violation in check_round(changes.files, worktree=worktree, settings=settings, project=project)]
    if planned:
        findings += _outside_plan(changes, set(planned), tests)
    for path, lines in changes.lines.items():
        if path not in approved:
            for action, changed in (("新增", lines.added), ("删除", lines.removed)):
                found = sorted({marker for line in changed for marker in rules.protected_markers if marker in line})
                findings += [Finding(CHECK, "protected_content", path, f"{action}了受保护标记 {marker}", NEEDS_USER)
                             for marker in found]
        markers = sorted({marker for line in lines.added for marker in rules.skip_markers if marker in line})
        findings += [Finding(CHECK, "skip_marker", path, f"新增了跳过标记 {marker}", LOCAL) for marker in markers]
        for line in lines.added:
            pattern = next((item.pattern for item in rules.residue if item.search(line)), None)
            if pattern is not None:
                findings.append(Finding(CHECK, "residue", path, f"新增行匹配残留模式 {pattern}：{line.strip()}", LOCAL))
    return findings


def hardcode_hints(changes: Changes, *, test_patterns: Sequence[str], issue_text: str,
                   min_length: int) -> list[HardcodeHint]:
    inputs = literals(issue_text, min_length)
    for path, lines in changes.lines.items():
        if matching_pattern(path, test_patterns) is not None:
            inputs |= {value for line in lines.added for value in literals(line, min_length)}
    hints = []
    for path, lines in sorted(changes.lines.items()):
        if matching_pattern(path, test_patterns) is not None:
            continue
        same = sorted({value for line in lines.added for value in literals(line, min_length)} & inputs)
        hints += [HardcodeHint(path, value) for value in same]
    return hints


def literals(text: str, min_length: int) -> set[str]:
    """不短于 min_length 的字符串与数字字面量：太短的(0、1、"id")处处都有，比了只会误报。"""
    found = set()
    for match in LITERAL.finditer(text):
        value = next((group for group in match.groups() if group is not None), "")
        if len(value) >= min_length:
            found.add(value)
    return found


def approved_files(facts: Mapping[str, object]) -> list[str]:
    """方案中写明并经确认要改的受保护文件(protectedTouches)。"""
    items = facts.get("protectedTouches") or []
    return [str(item["path"]) for item in items if isinstance(item, Mapping) and isinstance(item.get("path"), str)] \
        if isinstance(items, list) else []


def planned_files(facts: Mapping[str, object]) -> list[str]:
    """方案交接中的文件清单：`files` 每项为路径，或带 `path` 的对象。"""
    items = facts.get("files") or []
    paths = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            paths.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("path"), str):
            paths.append(str(item["path"]))
    return paths


def _boundary(kind: str, path: str | None, detail: str, over_cap_before: bool) -> Finding:
    if kind == OVER_CAP:
        if over_cap_before:
            return Finding(CHECK, kind, path, f"{detail}；{SIZE_REPEATED}", NEEDS_USER)
        return Finding(CHECK, kind, path, f"{detail}；{SIZE_GUIDANCE}", LOCAL)
    return Finding(CHECK, kind, path, detail, NEEDS_USER)  # 越界、禁改：不可由编码自行纠正


def _outside_plan(changes: Changes, planned: set[str], tests: Sequence[str]) -> list[Finding]:
    findings = []
    for change in changes.files:
        if change.path in planned:
            continue
        is_test = matching_pattern(change.path, tests) is not None
        if change.status == UNTRACKED:
            if not is_test:  # 新写的测试随改动一起提交，不算临时文件
                findings.append(Finding(CHECK, "temporary_file", change.path,
                                        "未跟踪且不在方案中的文件(临时文件)，交付前删除", LOCAL))
        elif is_test:
            action = "删除" if change.status == DELETED else "改动"
            findings.append(Finding(CHECK, "test_modified", change.path,
                                    f"{action}了方案没列出的已有测试：不能为了让测试通过去改测试", LOCAL))
        else:
            findings.append(Finding(CHECK, "outside_plan", change.path, "不在方案的文件清单中", PLAN_GAP))
    return findings
