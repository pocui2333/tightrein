"""单元测试共用的夹具：真实的 git 仓库与校验通过的 project.yaml；收集时按 slow_marks 给测试加 slow 标记。

git 仓库：临时目录中的裸仓库作为 origin，另 clone 一个工作仓库；git 以隔离的环境运行，不读取本机的全局与系统配置
(避免签名、hook 等个人设置影响测试)，作者与时区固定。
"""

import os
import subprocess
from pathlib import Path

import pytest
import slow_marks

from tightrein.config import project


GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": "/nonexistent",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Cui Ty",
    "GIT_AUTHOR_EMAIL": "cty@example.com",
    "GIT_COMMITTER_NAME": "Cui Ty",
    "GIT_COMMITTER_EMAIL": "cty@example.com",
    "TZ": "UTC",
    "LC_ALL": "C",
}


class GitRepos:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.environ = dict(GIT_ENV)

    def git(self, repo: Path, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=repo, env=self.environ, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} 失败：{result.stderr}")
        return result.stdout

    def write(self, repo: Path, path: str, text: str) -> Path:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def commit(self, repo: Path, message: str, files: dict[str, str] | None = None) -> str:
        for path, text in (files or {}).items():
            self.write(repo, path, text)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", message)
        return self.head(repo)

    def head(self, repo: Path) -> str:
        return self.git(repo, "rev-parse", "HEAD").strip()

    def origin_and_clone(self) -> tuple[Path, Path]:
        """origin 为裸仓库，main 上有一个提交；返回 (origin, 工作仓库)。"""
        origin = self.root / "origin.git"
        repo = self.root / "repo"
        self.root.mkdir(parents=True, exist_ok=True)
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        self.git(self.root, "init", "-q", "-b", "main", str(repo))
        self.git(repo, "remote", "add", "origin", str(origin))
        self.write(repo, ".gitignore", "bin/\nobj/\n")
        self.commit(repo, "chore: init", {
            "README.md": "# demo\n",
            "src/OrderService.cs": "class OrderService\n{\n    int Page = 1;\n}\n",
            "tests/OrderTests.cs": "class OrderTests {}\n",
        })
        self.git(repo, "push", "-q", "-u", "origin", "main")
        return origin, repo


@pytest.fixture
def repos(tmp_path):
    return GitRepos(tmp_path / "git")


PROJECT = {
    "project": {"name": "sample", "repo": "/Users/me/Projects/sample", "mainBranch": "main", "language": "zh"},
    "target": {"baseUrl": "https://staging.example.test", "healthcheck": "/api/health"},
    "accounts": {"roles": {"Company": {"keychain": "tightrein.sample.company"}},
                 "login": {"endpoint": "/api/login", "bodyTemplate": {}, "tokenPath": "data.token"}},
    "stages": {
        "triage": {"tool": "claude", "capability": "deep", "budgetPerDay": 5, "refuter": {"capability": "standard"},
                   "limits": {"maxTurns": 30, "maxDurationMs": 600000}},
        "fix": {"tool": "codex", "capability": "deep", "session": {"tool": "claude"},
                "review": {"light": {"capability": "deep"}, "deep": {"tool": "claude", "capability": "deep"}}},
        "improve": {"tool": "agy"},
    },
    "capabilities": {
        **{tier: {
            "claude": {"model": "claude-opus", "inputUsdPerMTok": 15, "outputUsdPerMTok": 75},
            "codex": {"model": "gpt-5", "inputUsdPerMTok": 1.25, "outputUsdPerMTok": 10},
        } for tier in ("deep", "strong")},
        "standard": {
            "claude": {"model": "claude-sonnet", "inputUsdPerMTok": 3, "outputUsdPerMTok": 15},
            "codex": {"model": "gpt-5-mini", "inputUsdPerMTok": 0.25, "outputUsdPerMTok": 2},
        },
        "light": {
            "claude": {"model": "claude-haiku", "inputUsdPerMTok": 1, "outputUsdPerMTok": 5},
            "codex": {"model": "gpt-5-nano", "inputUsdPerMTok": 0.05, "outputUsdPerMTok": 0.4},
        },
    },
    "evaluation": {"judge": {"runner": "codex", "capability": "deep"}, "budgetUsd": 20},
    "thresholds": {
        "suppressionDays": {"value": 30, "min": 7, "max": 90},
        "triage": {"deferredReopenOccurrences": {"value": 3, "min": 1, "max": 10}},
        "change": {"maxFiles": {"value": 3, "min": 1, "max": 10}, "maxLines": {"value": 20, "min": 5, "max": 300}},
        "autonomy": {"planMaxLines": {"value": 20, "min": 10, "max": 1000}},
    },
    "protectedPaths": ["Migrations/MigrationList.cs", "deploy/"],
    "protectedPatterns": ["[Authorize]", "[AllowAnonymous]"],
    "testPaths": ["tests/", "*.test.js"],
}


@pytest.fixture
def make_config(tmp_path):
    """按 PROJECT 构造校验通过的 ProjectConfig；changes 中的顶层键整体替换，值为 None 的键去掉。"""

    def build(**changes):
        data = {key: value for key, value in {**PROJECT, **changes}.items() if value is not None}
        return project.parse(data, tmp_path / "project.yaml")

    return build


def pytest_collection_modifyitems(config, items):
    slow_marks.apply(items, Path(__file__).parent)
