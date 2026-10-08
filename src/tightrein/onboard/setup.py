"""接入清单(setup.json)：每个阶段、每个模块是启用、不启用还是由项目自己实现。

`setup.json` 是唯一的来源，程序只读它决定每个模块跑不跑、用什么跑；`setup.md` 由 onboard/render.py 从它渲染。
校验：每个模块都必须明确列出(漏写报错，不按缺省悄悄处理)；按状态检查必填字段；custom 的脚本必须存在。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from tightrein.protocol.security import looks_like_credential
from tightrein.store.files.json import read_json
from tightrein.store.files.layout import WorkspaceLayout

MODULES: tuple[str, ...] = (
    "collect.project_probes",
    "collect.platform_errors",
    "collect.access_log",
    "collect.alerts",
    "collect.api_fuzz",
    "collect.static",
    "collect.incidental",
    "implement.check.runtime",
    "release.deploy",
    "release.accept",
    "release.github_issues",
)
FIELDS = ("status", "method", "script", "guide", "reason", "impact")
# 只是开关、没有取数程序可替换的模块：不能写 custom；不启用的影响是确定的(setup.md 写明)，不算盲区
SWITCHES = frozenset({"release.accept", "release.github_issues"})


class ModuleStatus(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    CUSTOM = "custom"


class SetupInvalid(Exception):
    def __init__(self, path: Path, issues: list[str]) -> None:
        super().__init__(f"{path} 有问题：\n" + "\n".join(f"- {issue}" for issue in issues))
        self.path = path
        self.issues = issues


@dataclass(frozen=True)
class ModuleSetup:
    key: str
    status: ModuleStatus
    method: str | None
    script: str | None
    guide: str | None
    reason: str | None
    impact: str | None
    secrets: tuple[str, ...] = ()  # 自定义脚本需要的凭据条目名；程序只注入这几项


@dataclass(frozen=True)
class Setup:
    project: str
    updated_at: str
    modules: dict[str, ModuleSetup]

    def module(self, key: str) -> ModuleSetup:
        if key not in self.modules:
            raise KeyError(f"接入清单中没有 {key}")
        return self.modules[key]

    def enabled(self, key: str) -> bool:
        return self.module(key).status is not ModuleStatus.DISABLED

    def blind_spots(self) -> list[ModuleSetup]:
        """不启用且没有兜底的项：status 与 setup.md 单独提醒。"""
        return [item for item in self.modules.values()
                if item.status is ModuleStatus.DISABLED and not item.impact and item.key not in SWITCHES]


def load(workspace: WorkspaceLayout) -> Setup:
    path = workspace.setup
    if not path.exists():
        raise SetupInvalid(path, ["文件不存在：先运行 tightrein project add"])
    data = read_json(path)
    issues = issues_of(data, workspace)
    if issues:
        raise SetupInvalid(path, issues)
    modules = {key: _module(key, value) for key, value in data["modules"].items()}
    return Setup(project=data["project"], updated_at=data["updatedAt"], modules=modules)


def issues_of(data: Any, workspace: WorkspaceLayout) -> list[str]:
    if not isinstance(data, dict):
        return ["顶层必须是对象"]
    issues = [f"{name}：缺少" for name in ("project", "updatedAt", "modules") if name not in data]
    modules = data.get("modules")
    if not isinstance(modules, dict):
        return issues + ["modules：必须是对象"]
    issues += [f"modules.{key}：漏写；每个模块都要明确列出" for key in MODULES if key not in modules]
    issues += [f"modules.{key}：不认识的模块" for key in modules if key not in MODULES]
    for key, value in modules.items():
        if key in MODULES:
            issues += [f"modules.{key}.{issue}" for issue in _module_issues(value, workspace)]
            if key in SWITCHES and isinstance(value, dict) and value.get("status") == ModuleStatus.CUSTOM.value:
                issues.append(f"modules.{key}.status：只能是 enabled 或 disabled(这一项只是开关)")
    return issues


def _module_issues(value: Any, workspace: WorkspaceLayout) -> list[str]:
    if not isinstance(value, dict):
        return ["必须是对象"]
    issues = [f"{name}：缺少(不适用写 null)" for name in FIELDS if name not in value]
    try:
        status = ModuleStatus(str(value.get("status")))
    except ValueError:
        return issues + ["status：只能是 enabled、disabled、custom"]
    required = {
        ModuleStatus.ENABLED: (),
        ModuleStatus.DISABLED: ("reason",),
        ModuleStatus.CUSTOM: ("script", "guide", "reason"),
    }[status]
    issues += [f"{name}：{status.value} 时必填" for name in required if not value.get(name)]
    script = value.get("script")
    if status is ModuleStatus.CUSTOM and script and not (workspace.root / script).is_file():
        issues.append(f"script：脚本不存在：{script}")
    for name in value.get("secrets") or []:
        if looks_like_credential(str(name)):
            issues.append("secrets：只写凭据条目名，不写值")
    return issues


def _module(key: str, value: dict[str, Any]) -> ModuleSetup:
    return ModuleSetup(
        key=key,
        status=ModuleStatus(value["status"]),
        method=value.get("method"),
        script=value.get("script"),
        guide=value.get("guide"),
        reason=value.get("reason"),
        impact=value.get("impact"),
        secrets=tuple(value.get("secrets") or ()),
    )
