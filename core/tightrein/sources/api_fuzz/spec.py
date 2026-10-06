"""接口描述(architecture/04 2.2 第 1 步、2.9)：经 spec-export 取得 data/specs/<commit>/openapi.json，按 commit 复用。

缓存与「只读 worktree 不在目标 commit」的判断由 extensions.client 完成，这里把结果归为三种情形：
- 可用：读出接口描述(JSON 且含 paths)；
- skipped：没有 spec-export 的实现，或扩展报告不适用；
- failed：实现需要只读 worktree(ExtensionClient.needs_repo)而 worktree 不存在或不在 release 且没有缓存(提示先同步
  只读 worktree)，或扩展返回错误、超时、输出不合格。接口描述不来自仓库时不需要 worktree。
另提供接口描述中的操作清单与扣除排除范围后的接口总数(coverage.endpointsTotal)。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.domain.enums import ExtensionPoint, RunStatus
from tightrein.extensions.client import ExtensionClient, WorktreeNotAtCommit
from tightrein.extensions.result import PointResult

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
SKIPPED_REASON = "未提供 spec-export 扩展"


@dataclass(frozen=True)
class SpecOutcome:
    status: RunStatus
    document: Mapping[str, Any] | None = None
    path: Path | None = None
    notes: tuple[str, ...] = ()
    extension: PointResult | None = None
    extensions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.document is not None


def ensure(client: ExtensionClient, repo: Path | None, release: str | None) -> SpecOutcome:
    if release is None:
        return SpecOutcome(RunStatus.FAILED, notes=("没有目标版本：deployments 表中没有成功的部署，也没有给出 --commit",))
    if repo is None and client.needs_repo(ExtensionPoint.SPEC_EXPORT):
        return SpecOutcome(RunStatus.FAILED, notes=("没有只读 worktree，无法取得接口描述",))
    try:
        result = client.spec_export(repo, release)
    except WorktreeNotAtCommit:
        return SpecOutcome(RunStatus.FAILED, notes=(
            f"只读 worktree 不在 {release} 且没有缓存的接口描述，先执行 tightrein project worktree sync --commit {release}",))
    entries = {result.point.value: result.stats_entry()}
    if result.failure is not None:
        return SpecOutcome(RunStatus.FAILED, notes=(f"spec-export 失败：{result.failure.describe()}", *result.notes),
                           extension=result, extensions=entries)
    if result.output is None:
        return SpecOutcome(RunStatus.SKIPPED, notes=result.notes or (SKIPPED_REASON,), extension=result,
                           extensions=entries)
    path = Path(result.output["specFile"])
    document = json.loads(path.read_text(encoding="utf-8"))
    return SpecOutcome(RunStatus.OK, document, path, result.notes, result, entries)


def operations(document: Mapping[str, Any]) -> list[tuple[str, str]]:
    """接口描述中的全部操作 (方法大写, 路由模板)，按路由与方法排序。"""
    found = []
    for route, item in document.get("paths", {}).items():
        if not isinstance(item, Mapping):
            continue
        for method in HTTP_METHODS:
            if method in item:
                found.append((method.upper(), route))
    return sorted(found, key=lambda pair: (pair[1], pair[0]))


def exclude_regex(patterns: Iterable[str]) -> str | None:
    """sources.api-fuzz.exclude 中各条正则以 | 合并为一条；没有时为空。"""
    items = [f"(?:{pattern})" for pattern in patterns]
    return "|".join(items) or None


def selected_operations(document: Mapping[str, Any], exclude: str | None) -> list[tuple[str, str]]:
    """扣除排除范围后的操作。"""
    pattern = None if exclude is None else re.compile(exclude)
    return [(method, route) for method, route in operations(document) if pattern is None or not pattern.search(route)]
