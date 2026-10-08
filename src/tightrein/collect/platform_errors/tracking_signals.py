"""错误追踪平台的分组 → 信号。

| 字段 | 取值 |
|---|---|
| check_type | error |
| location | 第一个本项目帧(出错处在前)的「文件:函数」；没有时为 culprit，再没有时为标题 |
| symbol | 该帧的函数 |
| message | 「异常类型: 消息」；没有异常类型时为平台标题 |
| occurred_at | 分组在平台上最近一次出现的时间 |
| commit | 发生时间之前最近一次成功部署的 commit；平台上的版本号另存 evidence.platformRelease |
| group_key | 平台分组编号 `sentry:<组织>/<编号>`(问题指纹) |

前端(浏览器端 SDK)的分组 evidence.kind 为 frontend，并带操作轨迹、页面地址与浏览器。
"""

from __future__ import annotations

from collections.abc import Iterable

from tightrein.collect.common.signals import ReleaseAt, Signal, SignalFactory
from tightrein.collect.platform_errors.error_tracking.sentry import StackFrame, TrackedIssue
from tightrein.protocol.naming import format_iso

SOURCE_NAME = "error_tracking"
CHECK_TYPE = "error"


def to_signals(issues: Iterable[TrackedIssue], factory: SignalFactory, release_at: ReleaseAt,
               frames: int) -> list[Signal]:
    """frames 为保留的本项目帧数。"""
    signals = []
    for issue in issues:
        project = [frame for frame in issue.frames if frame.in_app]
        first = next((frame for frame in project if frame.file or frame.function), None)
        signals.append(factory.create(
            check_type=CHECK_TYPE, location=location(issue), symbol=None if first is None else first.function,
            message=message(issue), occurred_at=issue.last_seen, commit=release_at(issue.last_seen),
            environment=issue.environment, group_key=issue.group,
            evidence={
                "sourceName": SOURCE_NAME,
                "kind": "frontend" if issue.frontend else "backend",
                "exceptionType": issue.type,
                "level": issue.level,
                "count": issue.count,
                "userCount": issue.user_count,
                "firstSeen": format_iso(issue.first_seen),
                "permalink": issue.permalink,
                "platformRelease": issue.release,
                "projectFrames": [{"symbol": frame.function or "", "file": frame.file, "line": frame.line}
                                  for frame in project[:frames]],
                **({"breadcrumbs": [{"time": crumb.time, "category": crumb.category, "message": crumb.message}
                                    for crumb in issue.breadcrumbs],
                    "url": issue.url, "browser": issue.browser} if issue.frontend else {}),
            },
        ))
    return signals


def location(issue: TrackedIssue) -> str:
    for frame in issue.frames:
        if frame.in_app:
            found = _frame_location(frame)
            if found:
                return found
    return issue.culprit or issue.title


def message(issue: TrackedIssue) -> str:
    if issue.type:
        return f"{issue.type}: {issue.message}" if issue.message else issue.type
    return issue.title


def _frame_location(frame: StackFrame) -> str | None:
    if frame.file and frame.function:
        return f"{frame.file}:{frame.function}"
    return frame.file or frame.function
