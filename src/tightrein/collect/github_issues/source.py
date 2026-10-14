"""GitHub 上已有 Issue 的采集：列出打开的 Issue → 去掉已关联本地 Issue 的与处理过的 → 每个转成一条信号。不调用模型。

- 只在 GitHub Issue 同步启用时跑(接入清单 release.github_issues，origin 是 GitHub 仓库)；它不是接入清单里的模块，
  由 collect.sources 按这个开关加进来源；
- 跳过：编号已是某个本地 Issue 的镜像的(自己建的、已接手的)；已处理过的(state 表 `collect.github_issues:seen`，
  编号列表，随信号在去重的同一个事务里保存，失败时不前进)；
- 类别：带 featureLabels 中任一标签的是需求(request)，其余当缺陷报告(report)；两种都是确定性来源(deterministic)：
  人报的 Issue 不会「再出现一次」，按偶发先观察就永远到不了评估。报告走完整取证；需求在评估里不取证，查重之后
  直接写成待修的用户需求；
- 位置：正文中第一个 `目录/文件.扩展名:行号`(反引号里的或裸写的；网址里的不算，含不带协议头的
  `127.0.0.1:8118/路径`)，没有为 None；指纹为 `github-issue:<编号>`，一个 GitHub Issue 一个问题，是否与别的问题
  同一根因交给评估的查重；
- 证据：编号、链接、标签、正文(先脱敏再截到 bodyChars)；覆盖范围永远为空(不会因为「没再出现」被判为已解决)。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from tightrein.assess.checks import locations_in
from tightrein.assess.issue import github as mirror
from tightrein.collect.common.signals import Signal, factory_for, releases, truncate
from tightrein.collect.common.source import SourceResult, SourceStatus, SourceUnavailable, skipped
from tightrein.protocol.git import GitError
from tightrein.protocol.handoff import Metrics
from tightrein.store.tables import issues, state

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

SOURCE = "collect.github_issues"
SEEN_KEY = f"{SOURCE}:seen"
REPORT, REQUEST = "report", "request"
GROUP_KEY = "github-issue:{number}"
NOT_ENABLED = "GitHub Issue 同步没有启用(接入清单 release.github_issues)，或 origin 不是 GitHub 仓库"
NOTHING_NEW = "没有新的打开的 Issue"
# 正文中的代码位置：至少带一层目录的 `路径:行号`，前面不能紧跟路径字符；网址里的由 locations_in 排除
LOCATION = re.compile(r"(?<![\w/.\-])((?:[\w.\-]+/)+[\w.\-]+\.[A-Za-z0-9]+):(\d+)\b")


def collect(runtime: Runtime) -> SourceResult:
    if not mirror.enabled(runtime):
        return skipped(SOURCE, NOT_ENABLED)
    assert runtime.github is not None
    try:
        found = runtime.github.open_issues()
    except GitError as error:
        raise SourceUnavailable(f"读不到 GitHub 上打开的 Issue：{error}") from error
    seen = {int(number) for number in state.get(runtime.conn, SEEN_KEY) or []}
    known = linked(runtime) | seen
    fresh = [item for item in found if item.number not in known]
    if not fresh:
        return SourceResult(SOURCE, SourceStatus.SKIPPED, [], len(found), None, NOTHING_NEW, {}, Metrics())
    section = runtime.settings.section(SOURCE)
    features = {str(label).lower() for label in section["featureLabels"]}
    factory = factory_for(runtime, SOURCE)
    release_at = releases(runtime.conn)
    now = runtime.clock.now()
    signals: list[Signal] = []
    for item in fresh:
        request = any(label.lower() in features for label in item.labels)
        at = item.created_at or now
        evidence: dict[str, Any] = {"number": item.number, "url": item.url, "labels": list(item.labels),
                                    "body": truncate(runtime.redactor.text(item.body), int(section["bodyChars"]))}
        signals.append(factory.create(
            check_type=REQUEST if request else REPORT, location=location_in(item.body), message=item.title or
            f"#{item.number}", occurred_at=at, commit=release_at(at), evidence=evidence,
            group_key=GROUP_KEY.format(number=item.number), deterministic=True))
    produced = {"open": len(found), "new": len(fresh), "requests": sum(s.check_type == REQUEST for s in signals)}
    return SourceResult(SOURCE, SourceStatus.DONE, signals, len(found), None, None,
                        {SEEN_KEY: sorted(seen | {item.number for item in fresh})}, Metrics(produced=produced))


def linked(runtime: Runtime) -> set[int]:
    """已是本地 Issue 镜像的 GitHub 编号(自己建的与已接手的)。"""
    numbers: set[int] = set()
    for record in issues.find(runtime.conn):
        found = record.extra.get(mirror.MIRROR)
        number = found.get("number") if isinstance(found, dict) else None
        if number is not None and str(number).isdigit():
            numbers.add(int(number))
    return numbers


def location_in(text: str) -> str | None:
    found = locations_in(text or "", LOCATION)
    return f"{found[0].group(1)}:{found[0].group(2)}" if found else None
