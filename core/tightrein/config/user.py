"""本机用户配置 `~/.config/tightrein/config.yaml`(architecture/01 5.3)，只放因人而异、不应写进工作区的值。

- `agents` 段是 agent 工具与模型的个人缺省(结构为 project.yaml 中 defaultTool、stages 的工具与模型部分、capabilities、
  roleCapabilities 的子集)，原样交给 config.project 作为用户 agent 层，位于技术栈层与 project.yaml 之间
  (项目需要时覆盖)；这里只检查结构与同一模型在各档中的价格一致，档的引用与评审独立性在读取 project.yaml 时按合并值检查；
- `network` 段是本机的网络代理，由 config.network 翻译成子进程环境与 HTTP 代理表。

文件不存在或为空时各项为空(notify.method 的缺省值在 config/defaults.yaml 中，由组装根合成)；存在时按
config/user-config.schema.json 严格检查，不认识的键与类型不符的值一次列出，键名写全，例如 `tools.claude.path`。
路径开头的 `~` 按传入的 home 展开，测试不依赖真实的主目录。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.config import layers
from tightrein.config.capabilities import Capabilities, CapabilityError
from tightrein.config.layers import ROOT_KEY, ConfigError, ConfigIssue
from tightrein.store.files.layout import UserLayout

SCHEMA = "config/user-config.schema.json"
NOTIFY_MACOS = "macos"
NOTIFY_NONE = "none"
NOTIFY_METHODS = (NOTIFY_MACOS, NOTIFY_NONE)


@dataclass(frozen=True)
class InstallTarget:
    """skill 安装到某个工具的位置与开关；path 为空时使用该工具的默认位置(architecture/09 6.1)。"""

    path: Path | None = None
    enabled: bool = True


@dataclass(frozen=True)
class UserConfig:
    path: Path
    branch_prefix: str | None = None
    default_workspace: Path | None = None
    notify_method: str | None = None
    tools: Mapping[str, Path] = field(default_factory=dict)
    install_targets: Mapping[str, InstallTarget] = field(default_factory=dict)
    agents: Mapping[str, Any] = field(default_factory=dict)
    network_proxy: str | None = None
    no_proxy: tuple[str, ...] = ()

    def tool_path(self, tool: str) -> Path | None:
        """工具可执行文件的路径(agent 工具与 semgrep)；没有配置时返回 None，由调用方决定缺省值。"""
        return self.tools.get(tool)

    def install_target(self, tool: str) -> InstallTarget:
        return self.install_targets.get(tool, InstallTarget())


def default_path(home: Path) -> Path:
    return UserLayout(home).config()


def _path(text: str, home: Path) -> Path:
    if text == "~" or text.startswith("~/"):
        return home / text[2:]
    return Path(text)


def parse(data: Any, path: Path, home: Path) -> UserConfig:
    """按 config/user-config.schema.json 校验后转换；不合格时一次列出全部问题，键名写全。"""
    if data is None:
        return UserConfig(path)
    if not isinstance(data, Mapping):
        raise ConfigError(path, [ConfigIssue(ROOT_KEY, "顶层必须是映射")])
    issues = sorted({issue for error in layers.validator(SCHEMA).iter_errors(data)
                     for issue in layers.schema_issues(error)})
    if issues:
        raise ConfigError(path, issues)
    agents = data.get("agents", {})
    try:
        Capabilities(agents.get("capabilities"))
    except CapabilityError as error:
        raise ConfigError(path, [ConfigIssue(f"agents.{error.key}", error.reason)]) from error
    workspace = data.get("defaultWorkspace")
    tools = {tool: _path(item["path"], home) for tool, item in data.get("tools", {}).items()}
    targets = {tool: InstallTarget(_path(item["path"], home) if "path" in item else None, item.get("enabled", True))
               for tool, item in data.get("install", {}).get("targets", {}).items()}
    network = data.get("network", {})
    return UserConfig(path, data.get("branchPrefix"), None if workspace is None else _path(workspace, home),
                      data.get("notify", {}).get("method"), tools, targets, agents,
                      network.get("proxy"), tuple(network.get("noProxy", ())))


def load(path: Path | None = None, home: Path | None = None) -> UserConfig:
    """读取本机用户配置；path 缺省为 `<home>/.config/tightrein/config.yaml`，home 缺省为当前用户的主目录。"""
    home = Path.home() if home is None else home
    path = default_path(home) if path is None else path
    if not path.is_file():
        return UserConfig(path)
    return parse(layers.read_yaml(path), path, home)
