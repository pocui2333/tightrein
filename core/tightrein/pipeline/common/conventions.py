"""项目的分支、提交与 PR 约定(redesign/07-release.md 第 1 节)：按优先级取格式，并给出从历史推断的结果。

优先级从高到低：
1. 工作区 project.yaml 的 git.conventions.branch、git.conventions.commit(格式串，占位符见 domain/release_format.py)与
   git.conventions.prTemplate(仓库内的 PR 模板路径)；
2. 项目中写明的约定：PR 模板文件(TEMPLATE_FILES，第一个存在的)；commitlint 配置继承 config-conventional 时提交采用
   Conventional Commits；CONTRIBUTING.md、AGENTS.md、CLAUDE.md 中同一行写了分支(或提交)并在反引号中给出带占位符的模板，
   占位符全部能对应到格式的字段、全文只有一个这样的模板时采用，认不出或有多个不同模板时不采用；
3. 从历史推断(infer)：只给出结果供接入时确认，不自动采用；
4. 通用格式。
个人前缀：git.personalPrefix 为真，或采用的分支模板含个人前缀时，在分支名前加用户配置的 branchPrefix。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain import release_format
from tightrein.domain.release_format import BRANCH_PLACEHOLDERS, COMMIT_PLACEHOLDERS, GENERIC_BRANCH, GENERIC_COMMIT

WORKSPACE, PROJECT, GENERIC = "workspace", "project", "generic"
TEMPLATE_FILES = (".github/PULL_REQUEST_TEMPLATE.md", ".github/pull_request_template.md",
                  "docs/PULL_REQUEST_TEMPLATE.md", "docs/pull_request_template.md", "PULL_REQUEST_TEMPLATE.md",
                  "pull_request_template.md", ".gitlab/merge_request_templates/Default.md",
                  ".bitbucket/PULL_REQUEST_TEMPLATE.md")
COMMITLINT_FILES = ("commitlint.config.js", "commitlint.config.cjs", "commitlint.config.mjs", "commitlint.config.ts",
                    ".commitlintrc", ".commitlintrc.json", ".commitlintrc.yaml", ".commitlintrc.yml",
                    ".commitlintrc.js", ".commitlintrc.cjs")
CONVENTIONAL = "config-conventional"
DOC_FILES = ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md")
BRANCH_WORDS = ("分支", "branch", "ブランチ")
COMMIT_WORDS = ("提交", "commit", "コミット")
CODE = re.compile(r"`([^`\n]+)`")
DOC_PLACEHOLDER = re.compile(r"<([^<>]+)>|\{([^{}]+)\}")
# 文档模板中的占位符名称 → 格式字段(小写比较，含其中任一词即可)
BRANCH_NAMES = (("prefix", ("prefix", "前缀", "user", "用户", "姓名", "名前")),
                ("issue", ("issue", "编号", "number", "id", "ticket", "工单", "番号")),
                ("type", ("type", "类型", "kind", "種別", "タイプ")),
                ("slug", ("desc", "描述", "简述", "slug", "summary", "主题", "名称", "説明", "概要", "topic")))
COMMIT_NAMES = (("scope", ("scope", "范围", "模块", "module", "スコープ")),
                ("type", ("type", "类型", "種別", "タイプ")),
                ("summary", ("subject", "summary", "description", "desc", "描述", "简述", "说明", "一句话", "概要",
                             "説明", "message")))
TYPES = "feat|fix|feature|bugfix|hotfix|chore|refactor|docs"
CONVENTIONAL_SUBJECT = re.compile(r"^[a-z]+(\([^)]+\))?!?: \S")
BRANCH_SHAPES = (
    ("{prefix}{type}/{issue}-{slug}", re.compile(r"^[a-z0-9]+/[a-z]+/\d+-[a-z0-9-]+$")),
    ("{prefix}{type}/{slug}", re.compile(r"^[a-z0-9]+/[a-z]+/[a-z0-9-]+$")),
    ("{prefix}{type}/{issue}-{slug}", re.compile(r"^[a-z]+/\d+-[a-z0-9-]+$")),
    ("{prefix}{type}-{slug}", re.compile(rf"^[a-z0-9]+/(?:{TYPES})-[a-z0-9-]+$")),
    ("{prefix}{type}/{slug}", re.compile(rf"^(?:{TYPES})/[a-z0-9-]+$")),
)
MAIN_BRANCHES = frozenset({"main", "master", "develop", "dev", "HEAD"})


@dataclass(frozen=True)
class Conventions:
    branch: str
    commit: str
    template: str | None
    personal_prefix: bool
    sources: Mapping[str, str] = field(default_factory=dict)  # branch、commit、template → 来源与依据

    @property
    def branch_pattern(self) -> re.Pattern[str]:
        return release_format.branch_pattern(self.branch)


def _read(repo: Path, name: str) -> str | None:
    path = repo / name
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None


def _convert(text: str, names: Sequence[tuple[str, Sequence[str]]], allowed: Sequence[str]) -> str | None:
    """把文档中的模板(如 `<类型>/<编号>-<简述>`)换成格式串；有认不出的占位符时为 None。"""
    failed = False

    def field_of(match: re.Match[str]) -> str:
        nonlocal failed
        word = (match.group(1) or match.group(2)).strip().lower()
        found = next((key for key, words in names if any(item in word for item in words)), None)
        if found is None or found not in allowed:
            failed = True
            return ""
        return "{" + found + "}"

    converted = DOC_PLACEHOLDER.sub(field_of, text.strip())
    if failed or not DOC_PLACEHOLDER.search(text):
        return None
    if "{prefix}/" in converted:
        converted = converted.replace("{prefix}/", "{prefix}")
    converted = converted.replace("({scope})", "{scope}")
    return converted


def _doc_template(repo: Path, words: Sequence[str], names: Sequence[tuple[str, Sequence[str]]],
                  allowed: Sequence[str], required: str) -> tuple[str, str] | None:
    """各说明文档中关于分支(或提交)的模板；全部文档只有一个不同的模板时返回 (格式串, 出处)。"""
    found: dict[str, str] = {}
    for name in DOC_FILES:
        text = _read(repo, name)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if not any(word in line.lower() for word in words):
                continue
            for code in CODE.findall(line):
                converted = _convert(code, names, allowed)
                if converted is not None and "{" + required + "}" in converted:
                    found.setdefault(converted, f"{name}:{number}")
    if len(found) != 1:
        return None
    return next(iter(found.items()))


def _commitlint(repo: Path) -> str | None:
    for name in COMMITLINT_FILES:
        text = _read(repo, name)
        if text is not None and CONVENTIONAL in text:
            return name
    package = _read(repo, "package.json")
    if package is not None:
        try:
            data = json.loads(package)
        except ValueError:
            return None
        if CONVENTIONAL in json.dumps(data.get("commitlint") or {}):
            return "package.json"
    return None


def resolve(config: ProjectConfig, repo: Path) -> Conventions:
    """按优先级取本项目的格式；sources 写明每一项的来源，供 PR 与运行摘要说明。"""
    get = config.get
    sources: dict[str, str] = {}
    branch = get("git.conventions.branch")
    if branch:
        sources["branch"] = WORKSPACE
    else:
        found = _doc_template(repo, BRANCH_WORDS, BRANCH_NAMES, BRANCH_PLACEHOLDERS, "slug")
        branch, sources["branch"] = (found[0], f"{PROJECT}:{found[1]}") if found else (GENERIC_BRANCH, GENERIC)
    commit = get("git.conventions.commit")
    if commit:
        sources["commit"] = WORKSPACE
    else:
        found = _doc_template(repo, COMMIT_WORDS, COMMIT_NAMES, COMMIT_PLACEHOLDERS, "summary")
        linted = _commitlint(repo)
        if found:
            commit, sources["commit"] = found[0], f"{PROJECT}:{found[1]}"
        elif linted:
            commit, sources["commit"] = GENERIC_COMMIT, f"{PROJECT}:{linted}"
        else:
            commit, sources["commit"] = GENERIC_COMMIT, GENERIC
    template_path = get("git.conventions.prTemplate")
    template = _read(repo, template_path) if template_path else None
    if template is not None:
        sources["template"] = f"{WORKSPACE}:{template_path}"
    else:
        name = next((item for item in TEMPLATE_FILES if (repo / item).is_file()), None)
        template = _read(repo, name) if name else None
        sources["template"] = f"{PROJECT}:{name}" if name else GENERIC
    release_format.check_format(branch, BRANCH_PLACEHOLDERS)
    release_format.check_format(commit, COMMIT_PLACEHOLDERS)
    prefixed = bool(get("git.personalPrefix")) or (sources["branch"] != GENERIC and "{prefix}" in branch)
    return Conventions(branch, commit, template, prefixed, sources)


# 从历史推断

@dataclass(frozen=True)
class Inferred:
    """一项推断：格式(没有统一风格时为空)、符合的比例与样本。"""

    format: str | None
    ratio: float
    samples: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"format": self.format, "ratio": round(self.ratio, 2), "samples": list(self.samples)}


@dataclass(frozen=True)
class Inference:
    """从最近的提交信息与远程分支名推断出的风格，供接入时交用户确认后写入 git.conventions。"""

    commit: Inferred
    branch: Inferred

    def to_dict(self) -> dict[str, Any]:
        return {"commit": self.commit.to_dict(), "branch": self.branch.to_dict()}


SAMPLE_SHOWN = 5


def infer(subjects: Sequence[str], branches: Sequence[str], *, min_ratio: float, min_samples: int) -> Inference:
    """提交：Conventional Commits 的比例达到 min_ratio 时为通用提交格式。分支：最常见的形状达到比例时为它的格式。
    样本少于 min_samples 时不推断。"""
    commits = [subject for subject in subjects if subject and not subject.startswith("Merge ")]
    matched = [subject for subject in commits if CONVENTIONAL_SUBJECT.match(subject)]
    ratio = len(matched) / len(commits) if commits else 0.0
    commit_format = GENERIC_COMMIT if len(commits) >= min_samples and ratio >= min_ratio else None
    named = [name for name in branches if name not in MAIN_BRANCHES]
    shapes: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    for name in named:
        shape = next((fmt for fmt, pattern in BRANCH_SHAPES if pattern.match(name)), None)
        if shape is not None:
            shapes[shape] += 1
            examples.setdefault(shape, []).append(name)
    branch_format, share, shown = None, 0.0, ()
    if shapes and named:
        best, count = shapes.most_common(1)[0]
        share = count / len(named)
        shown = tuple(examples[best][:SAMPLE_SHOWN])
        if len(named) >= min_samples and share >= min_ratio:
            branch_format = best
    return Inference(Inferred(commit_format, ratio, tuple(matched[:SAMPLE_SHOWN])),
                     Inferred(branch_format, share, shown))


def infer_from_repo(git: Any, repo: Path, config: ProjectConfig) -> Inference:
    """读取最近 git.inference.sampleSize 条提交与全部远程分支名后推断(只读)。"""
    size = int(config.get("git.inference.sampleSize"))
    subjects = [commit.subject for commit in git.log(repo, "HEAD", limit=size)]
    return infer(subjects, git.remote_branches(repo), min_ratio=float(config.get("git.inference.minRatio")),
                 min_samples=int(config.get("git.inference.minSamples")))
