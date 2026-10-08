"""探测仓库(接入第 2 步)：只读文件与 git，不调用模型，生成 setup.json 与 settings.json 的草稿。

- 测试、lint、构建、类型检查命令：按仓库根的标记文件识别技术栈，从 package.json 的 scripts、pyproject.toml 的
  工具配置、Makefile 的目标取；识别不到的写 null；
- 主分支：远程有 main 或 master 的取它，否则取当前分支；
- 依赖里有没有 Sentry SDK、有没有接口描述文件、有没有 CI 配置与部署工作流；
- 提交与分支的风格从最近的提交历史与远程分支名推断(protocol/git/format.infer_from_repo)，只写进 setup.md 与
  命令输出给用户确认，不自动写进 settings；
- 探测不到的一律写 null(不省略、不猜)，由 setup.md 标出待填。
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.onboard.setup import FIELDS, MODULES
from tightrein.protocol.git import Git, GitError
from tightrein.protocol.git.format import Inference, Inferred, infer_from_repo

COMMAND_NAMES = ("test", "lint", "build", "typecheck")
STACK_MARKERS: dict[str, tuple[str, ...]] = {
    "python": ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile"),
    "node": ("package.json",),
    "go": ("go.mod",),
    "rust": ("Cargo.toml",),
    "java": ("pom.xml", "build.gradle", "build.gradle.kts"),
    "ruby": ("Gemfile",),
    "dotnet": ("*.sln", "*.csproj"),
}
# 没有更具体的线索时，各技术栈的通用命令
STACK_COMMANDS: dict[str, dict[str, str]] = {
    "go": {"test": "go test ./...", "lint": "go vet ./...", "build": "go build ./..."},
    "rust": {"test": "cargo test", "lint": "cargo clippy", "build": "cargo build"},
    "java": {"test": "mvn -q test"},
    "ruby": {"test": "bundle exec rake test"},
    "dotnet": {"test": "dotnet test", "build": "dotnet build"},
}
TEST_PATTERNS: dict[str, tuple[str, ...]] = {
    "python": ("tests/**", "test_*.py", "*_test.py"),
    "node": ("**/*.test.*", "**/*.spec.*", "**/__tests__/**"),
    "go": ("*_test.go",),
    "rust": ("tests/**",),
    "java": ("src/test/**",),
    "ruby": ("test/**", "spec/**"),
    "dotnet": ("*Tests/**",),
}
FRONTEND_PACKAGES = ("react", "vue", "svelte", "next", "nuxt", "@angular/core", "solid-js")
FRONTEND_PATTERNS = ("**/*.tsx", "**/*.jsx", "**/*.vue", "**/*.svelte", "**/*.css", "**/*.scss")
SENTRY_MARKERS = ("@sentry/", "sentry-sdk", "sentry_sdk", "getsentry/sentry-go", "sentry-ruby", "sentry-raven",
                  "io.sentry", "Sentry.AspNetCore")
DEPENDENCY_FILES = ("package.json", "pyproject.toml", "requirements.txt", "Pipfile", "go.mod", "Gemfile", "pom.xml",
                    "build.gradle", "build.gradle.kts", "Cargo.toml")
SPEC_FILES = ("openapi.yaml", "openapi.yml", "openapi.json", "swagger.yaml", "swagger.yml", "swagger.json",
              "docs/openapi.yaml", "docs/openapi.yml", "docs/openapi.json", "api/openapi.yaml", "api/openapi.json")
CI_FILES = (".gitlab-ci.yml", "Jenkinsfile", ".circleci/config.yml", "azure-pipelines.yml", "bitbucket-pipelines.yml")
DEPLOY_WORDS = ("deploy", "release", "publish")
LOCKFILE_MANAGERS = (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"))
MAIN_CANDIDATES = ("main", "master")
_MAKE_TARGET = re.compile(r"^([A-Za-z][\w-]*)\s*:(?!=)", re.MULTILINE)


@dataclass(frozen=True)
class Detection:
    repo: Path
    main_branch: str | None
    stacks: dict[str, list[str]]  # 技术栈 → 命中的标记文件
    commands: dict[str, str | None]  # test、lint、build、typecheck
    test_patterns: list[str]
    frontend_patterns: list[str]
    sentry: list[str]  # 依赖中有 Sentry SDK 的文件
    spec: str | None  # 接口描述文件(相对仓库根)
    ci: list[str]  # CI 配置文件
    deploy_workflows: list[str]
    github_remote: bool
    notes: list[str] = field(default_factory=list)  # 探测时遇到但不影响结果的问题(文件读不出等)
    conventions: Inference | None = None  # 从提交历史推断的提交与分支风格：只报告给用户确认，不自动采用


def detect(repo: Path, git: Git) -> Detection:
    notes: list[str] = []
    stacks = _stacks(repo)
    package = _json_file(repo / "package.json", notes)
    pyproject = _toml_file(repo / "pyproject.toml", notes)
    makefile = _make_targets(repo / "Makefile")
    commands = {name: None for name in COMMAND_NAMES} | _commands(repo, stacks, package, pyproject, makefile)
    workflows = sorted(path.relative_to(repo).as_posix() for pattern in ("*.yml", "*.yaml")
                       for path in (repo / ".github" / "workflows").glob(pattern))
    return Detection(
        repo=repo,
        main_branch=_main_branch(git, notes),
        stacks=stacks,
        commands=commands,
        test_patterns=sorted({pattern for stack in stacks for pattern in TEST_PATTERNS.get(stack, ())}),
        frontend_patterns=list(FRONTEND_PATTERNS) if _frontend(package) else [],
        sentry=[name for name in DEPENDENCY_FILES if _mentions(repo / name, SENTRY_MARKERS)],
        spec=next((name for name in SPEC_FILES if (repo / name).is_file()), None),
        ci=workflows + [name for name in CI_FILES if (repo / name).is_file()],
        deploy_workflows=[name for name in workflows if any(word in Path(name).name.lower() for word in DEPLOY_WORDS)],
        github_remote=_github_remote(git),
        notes=notes,
        conventions=_conventions(git, notes),
    )


def settings_draft(detection: Detection, language: str) -> dict[str, Any]:
    """工作区 settings.json 的草稿：项目事实(探测不到为 null)，覆盖部分留空。仓库写绝对路径，工作区搬家不受影响。"""
    return {
        "project": {
            "repo": str(detection.repo.resolve()),
            "mainBranch": detection.main_branch,
            "language": language,
            "commands": detection.commands,
            "testPatterns": detection.test_patterns or None,
            "frontendPatterns": detection.frontend_patterns or None,
        },
        "overrides": {},
    }


def setup_draft(detection: Detection, project: str, updated_at: str) -> dict[str, Any]:
    """setup.json 的草稿：探测得到的模块给出状态，其余状态写 null 待用户定(setup.load 会报出来)。"""
    modules = {key: dict.fromkeys(FIELDS) for key in MODULES}
    known = {
        "collect.static": {"status": "enabled"},
        "collect.incidental": {"status": "enabled"},
        "release.accept": {"status": "enabled"},
    }
    if detection.sentry:
        known["collect.platform_errors"] = {"status": "enabled", "method": "sentry"}
    if detection.spec is not None:
        known["collect.api_fuzz"] = {"status": "enabled"}
    if detection.deploy_workflows:
        known["release.deploy"] = {"status": "enabled", "method": "github_actions"}
    if detection.github_remote:
        known["release.github_issues"] = {"status": "enabled"}
    for key, values in known.items():
        modules[key].update(values)
    return {"project": project, "updatedAt": updated_at, "modules": modules}


def convention_lines(inference: Inference | None) -> list[str]:
    """推断结果写给人看(setup.md 与命令输出)：推断出的格式要用户确认后自己写进 settings.json，程序不自动采用。"""
    if inference is None:
        return []
    return [_convention_line("提交", "commit", inference.commit), _convention_line("分支", "branch", inference.branch)]


def hints(detection: Detection) -> dict[str, str]:
    """写进 setup.md 的探测依据：模块键 → 一句话。"""
    found: dict[str, str] = {}
    if detection.sentry:
        found["collect.platform_errors"] = "Sentry SDK：" + "、".join(detection.sentry)
    if detection.spec is not None:
        found["collect.api_fuzz"] = f"接口描述：{detection.spec}"
    if detection.deploy_workflows:
        found["release.deploy"] = "部署工作流：" + "、".join(detection.deploy_workflows)
    return found


# 内部


def _stacks(repo: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for stack, patterns in STACK_MARKERS.items():
        hits = sorted({path.name for pattern in patterns for path in repo.glob(pattern) if path.is_file()})
        if hits:
            found[stack] = hits
    return found


def _commands(repo: Path, stacks: dict[str, list[str]], package: dict[str, Any], pyproject: dict[str, Any],
              makefile: set[str]) -> dict[str, str | None]:
    """先取项目自己写明的(Makefile 目标、package.json 的 scripts)，再按工具配置推断，最后取技术栈的通用命令。"""
    found: dict[str, str | None] = {}
    for stack in stacks:
        for name, command in STACK_COMMANDS.get(stack, {}).items():
            found.setdefault(name, command)
    if pyproject or "python" in stacks:
        found.update(_python_commands(repo, pyproject))
    declared = package.get("scripts")
    scripts: dict[str, Any] = declared if isinstance(declared, dict) else {}
    manager = next((name for lockfile, name in LOCKFILE_MANAGERS if (repo / lockfile).is_file()), "npm")
    for name, aliases in (("test", ("test",)), ("lint", ("lint",)), ("build", ("build",)),
                          ("typecheck", ("typecheck", "type-check", "tsc"))):
        script = next((alias for alias in aliases if alias in scripts), None)
        if script is not None:
            found[name] = f"{manager} test" if script == "test" else f"{manager} run {script}"
    for name in COMMAND_NAMES:
        if name in makefile:
            found[name] = f"make {name}"
    return found


def _python_commands(repo: Path, pyproject: dict[str, Any]) -> dict[str, str]:
    tools = pyproject.get("tool") or {}
    text = json.dumps(pyproject) + _read(repo / "requirements.txt") + _read(repo / "requirements-dev.txt")
    found: dict[str, str] = {}
    if "pytest" in tools or "pytest" in text or (repo / "pytest.ini").is_file() or (repo / "tests").is_dir():
        found["test"] = f"{_python(repo)} -m pytest -q"
    if "ruff" in tools or "ruff" in text:
        found["lint"] = "ruff check ."
    elif "flake8" in text or (repo / ".flake8").is_file():
        found["lint"] = "flake8"
    if "mypy" in tools or "mypy" in text:
        found["typecheck"] = "mypy ."
    return found


def _python(repo: Path) -> str:
    """项目自己的 .venv 解释器优先(写绝对路径：检查在不含 .venv 的工作树里跑)，否则 python3(macOS 没有 python)。"""
    venv = repo / ".venv" / "bin" / "python"
    return shlex.quote(str(venv.absolute())) if venv.exists() else "python3"


def _frontend(package: dict[str, Any]) -> bool:
    names: set[str] = set()
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        value = package.get(section)
        if isinstance(value, dict):
            names |= set(value)
    return any(name in names for name in FRONTEND_PACKAGES)


def _main_branch(git: Git, notes: list[str]) -> str | None:
    try:
        remote = set(git.remote_branches())
        for name in MAIN_CANDIDATES:
            if name in remote:
                return name
        return git.head().branch
    except GitError as error:
        notes.append(f"读取主分支失败：{error}")
        return None


def _conventions(git: Git, notes: list[str]) -> Inference | None:
    try:
        return infer_from_repo(git)
    except GitError as error:
        notes.append(f"从提交历史推断约定失败：{error}")
        return None


def _convention_line(label: str, key: str, found: Inferred) -> str:
    if found.format is None:
        return f"{label}：历史中没有统一的风格(最多的一种占 {found.ratio:.0%})，用通用格式"
    samples = f"，例：{'、'.join(found.samples)}" if found.samples else ""
    return (f"{label}：`{found.format}`(占 {found.ratio:.0%}{samples})；要采用就写进 settings.json 的 "
            f"overrides.git.{key}，程序不自动采用")


def _github_remote(git: Git) -> bool:
    try:
        url = git.remote_url()
    except GitError:
        return False
    return url is not None and "github.com" in url


def _make_targets(path: Path) -> set[str]:
    return set(_MAKE_TARGET.findall(_read(path)))


def _mentions(path: Path, markers: tuple[str, ...]) -> bool:
    text = _read(path)
    return any(marker in text for marker in markers)


def _json_file(path: Path, notes: list[str]) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        notes.append(f"{path.name} 读不出：{type(error).__name__}")
        return {}
    return value if isinstance(value, dict) else {}


def _toml_file(path: Path, notes: list[str]) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        notes.append(f"{path.name} 读不出：{type(error).__name__}")
        return {}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
