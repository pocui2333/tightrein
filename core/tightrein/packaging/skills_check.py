"""skill 的机械检查(architecture/09 5.1)，在单元测试与 install 前运行。

- frontmatter 只有 name 与 description；name 与目录名相同，只含小写字母、数字与连字符；description 非空且不超过
  1024 个字符(Agent Skills 标准的上限)；
- SKILL.md 正文(frontmatter 之后)的行数不超过配置的上限；
- SKILL.md 引用的 references/ 文件存在；参考文件不再引用其他参考文件，引用只一层深；
- references/ 中的文件都被 SKILL.md 引用，由核心作为任务说明加载的 references/roles/、references/tasks/ 除外；
  超过 packaging.referenceTocLines 行的参考文件有「## 目录」一节；
- SKILL.md 与参考文件中出现的 `tightrein <命令>` 存在于命令树中，有子命令的命令另核对子命令。
没有 SKILL.md 的目录不是 skill；skip 中的名称(锁定的第三方 skill)不检查。
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

from tightrein.store.files import yaml_text

SKILL_FILE = "SKILL.md"
REFERENCES = "references"
FRONTMATTER_FENCE = "---"
FRONTMATTER_KEYS = frozenset({"name", "description"})
DESCRIPTION_MAX = 1024
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
REFERENCE = re.compile(r"references/[A-Za-z0-9_./-]+\.md")
CORE_LOADED_DIRS = ("roles", "tasks")
TOC_HEADING = "## 目录"
MARKDOWN_LINK = re.compile(r"\]\((?!https?:)([^)#]+\.md)\)")
COMMAND = re.compile(r"tightrein[ \t]+([a-z][a-z-]*)(?:[ \t]+([a-z][a-z-]*))?")

CommandTree = Mapping[str, frozenset[str] | None]


@dataclass(frozen=True)
class SkillIssue:
    skill: str
    path: Path
    rule: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"skill": self.skill, "path": str(self.path), "rule": self.rule, "detail": self.detail}


def command_tree(parser: argparse.ArgumentParser) -> dict[str, frozenset[str] | None]:
    """顶层命令 → 子命令集合；没有子命令的为 None。有子命令的顶层命令都不接受其他位置参数，后面的小写单词只能是子命令。"""
    tree: dict[str, frozenset[str] | None] = {}
    for action in parser._actions:  # noqa: SLF001 argparse 没有公开遍历子命令的接口
        if not isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            continue
        for name, child in action.choices.items():
            groups = [item for item in child._actions  # noqa: SLF001
                      if isinstance(item, argparse._SubParsersAction)]  # noqa: SLF001
            tree[name] = frozenset(groups[0].choices) if groups else None
    return tree


def split(text: str) -> tuple[str | None, str]:
    """frontmatter 文本与正文；没有 frontmatter 时前者为 None。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_FENCE:
        return None, text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == FRONTMATTER_FENCE:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1:])
    return None, text


def _frontmatter(name: str, path: Path, header: str | None) -> list[SkillIssue]:
    if header is None:
        return [SkillIssue(name, path, "frontmatter", "缺少以 --- 包围的 frontmatter")]
    try:
        data = yaml_text.load(header)
    except yaml_text.YamlError as error:
        return [SkillIssue(name, path, "frontmatter", str(error))]
    if not isinstance(data, Mapping) or set(data) != FRONTMATTER_KEYS:
        keys = sorted(map(str, data)) if isinstance(data, Mapping) else []
        return [SkillIssue(name, path, "frontmatter", f"只能有 name 与 description，实际为 {keys}")]
    issues = []
    if data["name"] != name or not NAME.match(str(data["name"])):
        issues.append(SkillIssue(name, path, "name", f"name 须与目录名 {name} 相同，只含小写字母、数字与连字符"))
    description = data["description"]
    if not isinstance(description, str) or not description.strip() or len(description) > DESCRIPTION_MAX:
        issues.append(SkillIssue(name, path, "description", f"description 须为非空文本，不超过 {DESCRIPTION_MAX} 个字符"))
    return issues


def _commands(name: str, path: Path, text: str, commands: CommandTree) -> list[SkillIssue]:
    issues = []
    for match in COMMAND.finditer(text):
        command, sub = match.group(1), match.group(2)
        if command not in commands:
            issues.append(SkillIssue(name, path, "command", f"命令树中没有 tightrein {command}"))
            continue
        subcommands = commands[command]
        if subcommands is not None and sub is not None and sub not in subcommands:
            issues.append(SkillIssue(name, path, "command", f"命令树中没有 tightrein {command} {sub}"))
    return issues


def _references(name: str, directory: Path, body: str, commands: CommandTree, toc_lines: int) -> list[SkillIssue]:
    issues = []
    cited_by_skill = set(REFERENCE.findall(body))
    for cited in sorted(cited_by_skill):
        if not (directory / cited).is_file():
            issues.append(SkillIssue(name, directory / SKILL_FILE, "reference", f"引用的 {cited} 不存在"))
    references = directory / REFERENCES
    if not references.is_dir():
        return issues
    for path in sorted(references.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(directory).as_posix()
        if path.relative_to(references).parts[0] not in CORE_LOADED_DIRS and relative not in cited_by_skill:
            issues.append(SkillIssue(name, path, "reference-unlisted", f"{relative} 没有被 SKILL.md 引用"))
        if len(text.splitlines()) > toc_lines and TOC_HEADING not in text.splitlines():
            issues.append(SkillIssue(name, path, "reference-toc", f"超过 {toc_lines} 行，缺少「{TOC_HEADING}」"))
        cited = sorted(set(REFERENCE.findall(text)) | set(MARKDOWN_LINK.findall(text)))
        if cited:
            issues.append(SkillIssue(name, path, "reference-depth", f"参考文件不能再引用其他参考文件：{cited}"))
        issues += _commands(name, path, text, commands)
    return issues


def check(skills_dir: Path, commands: CommandTree, *, max_lines: int, toc_lines: int,
          skip: Collection[str] = ()) -> list[SkillIssue]:
    issues: list[SkillIssue] = []
    for directory in sorted(path for path in skills_dir.iterdir() if path.is_dir()):
        skill_file = directory / SKILL_FILE
        if directory.name in skip or not skill_file.is_file():
            continue
        name = directory.name
        header, body = split(skill_file.read_text(encoding="utf-8"))
        issues += _frontmatter(name, skill_file, header)
        lines = len(body.splitlines())
        if lines > max_lines:
            issues.append(SkillIssue(name, skill_file, "length", f"正文 {lines} 行，超过 {max_lines} 行"))
        issues += _commands(name, skill_file, body, commands)
        issues += _references(name, directory, body, commands, toc_lines)
    return issues
