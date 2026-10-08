"""生成协议层总览 protocol/README.md：每个文件管什么、取值在 settings 的哪里、主要缺省值(取自 settings/defaults.json)。

README 不手写：改了缺省值或加了协议文件后运行 `python -m tightrein.protocol.overview` 重新生成。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.store.files.atomic import write_text
from tightrein.store.files.json import read_json
from tightrein.store.files.layout import ToolLayout

README = Path(__file__).parent / "README.md"
CONTROLS_DEFAULT = "controls.*"


@dataclass(frozen=True)
class Entry:
    document: str
    program: str
    purpose: str
    keys: tuple[str, ...]  # settings 中的键(点分)；不可配置的为空


ENTRIES = (
    Entry(
        "boundaries.md",
        "boundaries.py",
        "边界与关卡：各阶段能读写什么、命令白名单、改动量上限、受保护文件(两级)、必须人工的关卡",
        (
            "boundaries.readCommands",
            "boundaries.changeCap",
            "boundaries.autoApprove",
            "boundaries.protected.forbidden",
            "boundaries.protected.highRisk",
            "boundaries.gates",
        ),
    ),
    Entry(
        "naming.md",
        "naming.py",
        "文件命名与排版：目录、文件名、编号、时间与时长的写法，Markdown 与 JSON 的排版",
        (),
    ),
    Entry(
        "handoff.md",
        "handoff.py",
        "交接文档：结论、必填事实、量化数据、备注四部分；哪些落盘；给人看的三种文档；校验",
        (),
    ),
    Entry(
        "limits.md",
        "limits.py",
        "运行时限：三级超时、轮数、没有进展就停、按失败类型处理、重试与退避、熔断、锁的心跳",
        ("limits.retry", "limits.breaker", "limits.timeouts", "limits.lock"),
    ),
    Entry(
        "resources.md",
        "resources.py",
        "资源：并发与限流、订阅额度与给用户留的余量、每个 Issue 的用量上限",
        ("resources.concurrency", "resources.quota", "resources.issueTokens", "resources.cacheReadWeight"),
    ),
    Entry(
        "recovery.md",
        "recovery.py",
        "恢复与控制：检查点续跑、写操作幂等、中断时就地收尾、启动时恢复、暂停、急停、接管",
        ("limits.lock.stale", "limits.shutdownGrace"),
    ),
    Entry(
        "security.md",
        "security.py",
        "安全：凭据、脱敏、子进程环境变量白名单、只读副本与快照比对、网络、外部内容注入、外部代码",
        (),
    ),
    Entry(
        "records.md",
        "records.py",
        "记录：事件记录链、版本、保留期与清理",
        ("records.retention",),
    ),
    Entry(
        "schedule.md",
        "schedule/",
        "调度：三种触发、按状态推进到关卡、运行锁、跑完必复盘",
        ("schedule.tick", "schedule.window", "schedule.every", "schedule.advanceTo"),
    ),
    Entry(
        "git.md",
        "git/",
        "git 规范：格式怎么确定(项目约定 → 历史推断 → 通用)、分支、提交、PR、不带 AI 署名",
        ("git.branch", "git.commit", "git.types", "git.pr"),
    ),
    Entry(
        "coding.md",
        "",
        "代码规范：给目标项目写代码时的通用最低要求",
        ("boundaries.changeCap",),
    ),
)

_INTRO = """# 协议层总览

> 本文件由 `overview.py` 从 `settings/defaults.json` 生成，不要手改；
> 改了缺省值后运行 `python -m tightrein.protocol.overview`。

协议层定跨所有阶段的全局规则：每一方面一个 md(是什么、怎么做、在哪配置、缺省值、设计依据)，旁边是执行它的同名程序。
协议层只放规则逻辑，读写一律调用 `store/`；实际的取值都在 `settings/`(见 `settings/README.md`)。

## 协议层与配置的分工

- `protocol/` 定义协议：统一的控制字段各是什么意思、怎么生效、缺省值多少，键名规则与继承顺序；
- `settings/` 放取值：`defaults.json`(随 tightrein 提交) → `controls.json`(本机) → 工作区 `settings.json`(项目)，
  后者覆盖前者；
- 控制键写成「阶段.模块.小步骤」，控制字段按「小步骤 → 模块 → 阶段 → `*`」继承(`naming.md`)。
"""


def render(defaults: Mapping[str, Any]) -> str:
    lines = [_INTRO, "## 文件", "", "| 文件 | 程序 | 管什么 | 取值在 settings 的哪里 | 主要缺省值 |"]
    lines.append("|---|---|---|---|---|")
    for entry in ENTRIES:
        keys = "<br>".join(f"`{key}`" for key in entry.keys) or "不可配置"
        values = "<br>".join(f"`{key.rsplit('.', 1)[-1]}`：{_brief(_lookup(defaults, key))}" for key in entry.keys)
        values = values or "—"
        program = f"`{entry.program}`" if entry.program else "—"
        lines.append(f"| `{entry.document}` | {program} | {entry.purpose} | {keys} | {values} |")
    lines += [
        "",
        "## 统一的控制字段",
        "",
        f"每个调用点按控制键取下面的字段；全局缺省在 `{CONTROLS_DEFAULT}`，各阶段、模块、小步骤只写与上级不同的。",
        "",
        "| 字段 | 全局缺省 |",
        "|---|---|",
    ]
    for name, value in defaults["controls"]["*"].items():
        lines.append(f"| `{name}` | {_brief(value)} |")
    return "\n".join(lines) + "\n"


def write(tool: ToolLayout, target: Path = README) -> Path:
    write_text(target, render(read_json(tool.defaults)))
    return target


def _lookup(data: Mapping[str, Any], key: str) -> Any:
    node: Any = data
    for part in key.split("."):
        node = node[part]  # 缺键即报错：总览中列出的键必须在缺省值文件里
    return node


def _brief(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "、".join(_brief(item) for item in value) if len(value) <= 4 else f"{len(value)} 项"
    if isinstance(value, dict):
        if not value:
            return "无"
        return "，".join(f"{name} {'{…}' if isinstance(item, dict) else _brief(item)}" for name, item in value.items())
    return str(value)


if __name__ == "__main__":
    print(write(ToolLayout.discover()))
