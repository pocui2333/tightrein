"""错误追踪平台的分组到信号的映射(redesign/01-collect.md 第 1 节)。

| 字段 | 取值 |
|---|---|
| source | error |
| check | 后端为 error，前端(浏览器端 SDK)为 frontend-error |
| location | 第一条本项目帧(出错处在前)的「文件:函数」；没有本项目帧时为 culprit，再没有时为标题 |
| message | 「异常类型: 消息」；没有异常类型时为平台标题 |
| occurred_at | 分组在平台上的最近出现时间 |
| release | 发生时间之前最近一次成功部署的 commit(平台上的版本号记在 context.platformRelease) |

context.platformGroup 为平台分组编号(作为问题指纹，见 domain/fingerprint.py)；sourceName 为 error-tracking；另有出现次数、
影响用户数、首次出现时间、平台链接、本项目帧、操作轨迹、页面地址与浏览器。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from tightrein.domain.clock import parse_iso
from tightrein.domain.enums import Source
from tightrein.domain.fingerprint import PLATFORM_GROUP
from tightrein.domain.signal import Signal
from tightrein.sources.common.signals import SignalFactory
from tightrein.sources.platform_errors.logs import ReleaseAt

SOURCE_NAME = "error-tracking"
BACKEND_CHECK = "error"
FRONTEND_CHECK = "frontend-error"


def _frame_location(frame: Mapping[str, Any]) -> str | None:
    file, function = frame.get("file"), frame.get("function")
    if file and function:
        return f"{file}:{function}"
    return file or function


def location(issue: Mapping[str, Any]) -> str:
    for frame in issue["frames"]:
        if frame["inApp"]:
            found = _frame_location(frame)
            if found:
                return found
    return issue.get("culprit") or issue["title"]


def message(issue: Mapping[str, Any]) -> str:
    if issue.get("type"):
        return f"{issue['type']}: {issue.get('message') or ''}".rstrip(": ") if issue.get("message") else issue["type"]
    return issue["title"]


def to_signals(issues: Iterable[Mapping[str, Any]], factory: SignalFactory, release_at: ReleaseAt,
               frames: int) -> list[Signal]:
    """frames 为保留的本项目帧数(runtime.sources.projectFrames)。"""
    signals = []
    for issue in issues:
        occurred = parse_iso(issue["lastSeen"])
        project = [{"symbol": frame.get("function") or "", "file": frame.get("file"), "line": frame.get("line")}
                   for frame in issue["frames"] if frame["inApp"]][:frames]
        frontend = issue["kind"] == "frontend"
        signals.append(factory.create(
            source=Source.ERROR, check=FRONTEND_CHECK if frontend else BACKEND_CHECK, location=location(issue),
            message=message(issue), occurred_at=occurred, release=release_at(occurred),
            context={
                PLATFORM_GROUP: issue["group"],
                "sourceName": SOURCE_NAME,
                "exceptionType": issue.get("type"),
                "level": issue.get("level"),
                "count": issue["count"],
                "userCount": issue["userCount"],
                "firstSeen": issue["firstSeen"],
                "permalink": issue.get("permalink"),
                "platformRelease": issue.get("release"),
                "environment": issue.get("environment"),
                "projectFrames": project,
                **({"breadcrumbs": list(issue["breadcrumbs"]), "url": issue.get("url"), "browser": issue.get("browser")}
                   if frontend else {}),
            },
        ))
    return signals
