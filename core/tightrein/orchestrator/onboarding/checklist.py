"""接入清单的纯判断(redesign/10-onboarding.md 第 2 节)：识别技术栈、各项的推荐答案。不读写存储，不访问网络。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DONE, BLOCKED, FAILED = "done", "blocked", "failed"
SYSTEM, USER = "system", "user"
OPENAPI_FILES = ("openapi.yaml", "openapi.yml", "openapi.json", "swagger.yaml", "swagger.yml", "swagger.json",
                 "docs/openapi.yaml", "docs/openapi.json", "api/openapi.yaml")
DEPLOY_WORDS = ("deploy", "release", "publish")


@dataclass(frozen=True)
class Answer:
    """一个问题的推荐答案：key 与 value 为写回 project.yaml 的键与值(为空表示不写配置，只记下决定)，text 为说明。"""

    text: str
    key: str | None = None
    value: Any = None


@dataclass(frozen=True)
class Outcome:
    """一项检查的结果；state 为 blocked 时 recommendation 为推荐答案。"""

    state: str
    detail: str
    recommendation: Answer | None = None
    reason: str = ""

    @property
    def owner(self) -> str:
        return USER if self.state == BLOCKED else SYSTEM


@dataclass(frozen=True)
class Item:
    id: str
    title: str
    outcome: Outcome


def stacks(repo: Path, markers: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """仓库根目录下命中的标记文件，按技术栈。"""
    found: dict[str, list[str]] = {}
    for stack, patterns in markers.items():
        hits = sorted({path.name for pattern in patterns for path in repo.glob(pattern) if path.is_file()})
        if hits:
            found[stack] = hits
    return found


def check_commands(found: Mapping[str, Sequence[str]], commands: Mapping[str, str]) -> list[dict[str, str]]:
    """没有配置检查命令时按识别到的技术栈推荐的命令。"""
    return [{"name": stack, "cwd": ".", "command": commands[stack]} for stack in found if stack in commands]


def openapi_file(repo: Path) -> str | None:
    return next((name for name in OPENAPI_FILES if (repo / name).is_file()), None)


def deploy_recommendation(repo: Path) -> Answer:
    """部署来源：有部署工作流时推荐 core/github-actions；有 vercel.json 时说明手动配置的写法；否则不接入。"""
    workflows = sorted(path.name for pattern in ("*.yml", "*.yaml")
                       for path in (repo / ".github" / "workflows").glob(pattern)
                       if any(word in path.name.lower() for word in DEPLOY_WORDS))
    if workflows:
        return Answer(f"用 GitHub Actions 的部署工作流 {workflows[0]} 跟踪部署", "extensions.deploy-source",
                      {"use": "core/github-actions", "options": {"workflow": workflows[0]}})
    if (repo / "vercel.json").is_file():
        return Answer("暂不接入，以合并时间加观察期为准；要接入 Vercel 时在 project.yaml 写 extensions.deploy-source: "
                      "{use: core/vercel, options: {projectId: <项目>, keychainItem: <钥匙串条目>}}")
    return Answer("不接入，以合并时间加观察期为准(release.deploy.observationHours)")
