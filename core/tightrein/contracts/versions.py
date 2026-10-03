"""各 schema 的整数版本号与逐级升级函数(architecture/01 3.2)。

读取旧版本的文档时，从文档记录的版本起逐级调用升级函数：每个函数把第 n 版的文档转为第 n + 1 版。
schema 内嵌另一个 schema 时(例如交接文档的外层与 outputs)，内层升级会让外层同时升一版，外层的升级函数调用内层的升级函数。
"""

from __future__ import annotations

import copy
from typing import Any, Callable

Document = dict[str, Any]
Upgrader = Callable[[Document], Document]


class VersionError(Exception):
    """版本号未登记、超出范围，或缺少某一级的升级函数。"""


class VersionRegistry:
    def __init__(self) -> None:
        self._current: dict[str, int] = {}
        self._upgraders: dict[tuple[str, int], Upgrader] = {}

    def declare(self, name: str, version: int) -> None:
        if version < 1:
            raise ValueError(f"版本号从 1 开始：{name} {version}")
        if name in self._current:
            raise ValueError(f"重复登记版本号：{name}")
        self._current[name] = version

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._current))

    def current(self, name: str) -> int:
        if name not in self._current:
            raise VersionError(f"未登记版本号的 schema：{name}")
        return self._current[name]

    def upgrader(self, name: str, from_version: int) -> Callable[[Upgrader], Upgrader]:
        """登记把第 from_version 版升到下一版的函数，用作装饰器。"""
        current = self.current(name)
        if not 1 <= from_version < current:
            raise VersionError(f"{name} 当前为第 {current} 版，不能登记从第 {from_version} 版起的升级函数")
        if (name, from_version) in self._upgraders:
            raise VersionError(f"{name} 第 {from_version} 版的升级函数已登记")

        def register(function: Upgrader) -> Upgrader:
            self._upgraders[(name, from_version)] = function
            return function

        return register

    def missing_steps(self) -> list[tuple[str, int]]:
        return [
            (name, version)
            for name, current in sorted(self._current.items())
            for version in range(1, current)
            if (name, version) not in self._upgraders
        ]

    def upgrade(self, name: str, document: Document, from_version: int) -> Document:
        """返回升级到当前版本的副本，不修改传入的文档。"""
        current = self.current(name)
        if not 1 <= from_version <= current:
            raise VersionError(f"{name} 没有第 {from_version} 版，当前为第 {current} 版")
        result = copy.deepcopy(document)
        for version in range(from_version, current):
            step = self._upgraders.get((name, version))
            if step is None:
                raise VersionError(f"{name} 缺少从第 {version} 版到第 {version + 1} 版的升级函数")
            result = step(result)
        return result


REGISTRY = VersionRegistry()


def current(name: str) -> int:
    return REGISTRY.current(name)


def upgrade(name: str, document: Document, from_version: int) -> Document:
    return REGISTRY.upgrade(name, document, from_version)


REGISTRY.declare("common.schema.json", 1)


REGISTRY.declare("handoff/envelope.schema.json", 2)
REGISTRY.declare("data/signal.schema.json", 1)
REGISTRY.declare("data/problem.schema.json", 1)
REGISTRY.declare("data/project-probe-input.schema.json", 1)
REGISTRY.declare("data/project-probe-output.schema.json", 1)


REGISTRY.declare("data/regression.schema.json", 1)
REGISTRY.declare("data/authz-model.schema.json", 1)
REGISTRY.declare("data/eval-case.schema.json", 1)
REGISTRY.declare("data/eval-report.schema.json", 1)


REGISTRY.declare("runner/roles/claim-verifier.schema.json", 1)
REGISTRY.declare("runner/roles/refuter.schema.json", 1)
REGISTRY.declare("runner/tasks/triage-dedup.schema.json", 1)


REGISTRY.declare("runner/roles/static-review.schema.json", 1)
REGISTRY.declare("runner/roles/fix-scout.schema.json", 1)
REGISTRY.declare("runner/roles/repro-test.schema.json", 1)
REGISTRY.declare("runner/roles/fix-executor.schema.json", 1)
REGISTRY.declare("runner/roles/frontend-designer.schema.json", 1)
REGISTRY.declare("runner/roles/spec-drafter.schema.json", 1)
REGISTRY.declare("runner/roles/knowledge-curator.schema.json", 1)
REGISTRY.declare("runner/roles/judge.schema.json", 1)
REGISTRY.declare("runner/roles/lesson-writer.schema.json", 1)
REGISTRY.declare("runner/roles/rule-writer.schema.json", 1)
REGISTRY.declare("runner/roles/improvement-writer.schema.json", 1)


REGISTRY.declare("handoff/outputs/collect.schema.json", 1)
REGISTRY.declare("handoff/outputs/aggregate.schema.json", 1)
REGISTRY.declare("handoff/outputs/triage.schema.json", 2)
REGISTRY.declare("handoff/outputs/issue.schema.json", 2)
REGISTRY.declare("handoff/outputs/loop.schema.json", 2)


REGISTRY.declare("handoff/outputs/fix-plan.schema.json", 1)
REGISTRY.declare("handoff/outputs/fix-review.schema.json", 1)
REGISTRY.declare("handoff/outputs/fix.schema.json", 1)
REGISTRY.declare("handoff/outputs/verify.schema.json", 1)
REGISTRY.declare("handoff/outputs/release.schema.json", 1)
REGISTRY.declare("handoff/outputs/learn.schema.json", 1)


REGISTRY.declare("runner/guard-report.schema.json", 1)
REGISTRY.declare("runner/runner-task.schema.json", 1)
REGISTRY.declare("runner/runner-result.schema.json", 1)
REGISTRY.declare("runner/transcript-event.schema.json", 1)
REGISTRY.declare("runner/replay-index.schema.json", 1)
REGISTRY.declare("runner/change-request.schema.json", 1)
REGISTRY.declare("data/pending-operation.schema.json", 1)


REGISTRY.declare("handoff/frontmatter/issue.schema.json", 1)
REGISTRY.declare("data/knowledge.schema.json", 1)
REGISTRY.declare("handoff/frontmatter/report.schema.json", 1)


REGISTRY.declare("config/project-config.schema.json", 1)
REGISTRY.declare("config/user-config.schema.json", 1)


REGISTRY.declare("extension/extension-request.schema.json", 1)
REGISTRY.declare("extension/extension-response.schema.json", 1)
REGISTRY.declare("extension/stack-manifest.schema.json", 1)
REGISTRY.declare("extension/points/spec-export.input.schema.json", 1)
REGISTRY.declare("extension/points/spec-export.output.schema.json", 1)
REGISTRY.declare("extension/points/authz-endpoints.input.schema.json", 1)
REGISTRY.declare("extension/points/authz-endpoints.output.schema.json", 1)
REGISTRY.declare("extension/points/authz-roles.input.schema.json", 1)
REGISTRY.declare("extension/points/authz-roles.output.schema.json", 1)
REGISTRY.declare("extension/points/error-tracking.input.schema.json", 1)
REGISTRY.declare("extension/points/error-tracking.output.schema.json", 1)
REGISTRY.declare("extension/points/log-platform.input.schema.json", 1)
REGISTRY.declare("extension/points/log-platform.output.schema.json", 1)
REGISTRY.declare("extension/points/alert-source.input.schema.json", 1)
REGISTRY.declare("extension/points/alert-source.output.schema.json", 1)
REGISTRY.declare("extension/points/log-parse.input.schema.json", 1)
REGISTRY.declare("extension/points/log-parse.output.schema.json", 1)
REGISTRY.declare("extension/points/static-tools.input.schema.json", 1)
REGISTRY.declare("extension/points/static-tools.output.schema.json", 1)
REGISTRY.declare("extension/points/page-routes.input.schema.json", 1)
REGISTRY.declare("extension/points/page-routes.output.schema.json", 1)
REGISTRY.declare("extension/points/local-run.input.schema.json", 1)
REGISTRY.declare("extension/points/local-run.output.schema.json", 1)
REGISTRY.declare("extension/points/deploy-source.input.schema.json", 1)
REGISTRY.declare("extension/points/deploy-source.output.schema.json", 1)


REGISTRY.declare("extension/method-manifest.schema.json", 1)


REGISTRY.declare("handoff/document.schema.json", 1)
REGISTRY.declare("handoff/types/task.schema.json", 1)
REGISTRY.declare("handoff/types/result.schema.json", 1)
REGISTRY.declare("handoff/types/finding.schema.json", 1)
REGISTRY.declare("handoff/types/decision.schema.json", 1)
REGISTRY.declare("handoff/types/plan.schema.json", 1)
REGISTRY.declare("handoff/types/review.schema.json", 1)
REGISTRY.declare("handoff/types/progress.schema.json", 1)
REGISTRY.declare("handoff/types/issue.schema.json", 1)


# 第 2 版：分诊的优先分、可修复度与容量推算改为处理标签、任务类型、规模档与预估改动

@REGISTRY.upgrader("handoff/outputs/triage.schema.json", 1)
def _triage_v1(document: Document) -> Document:
    for key in ("priorityScore", "fixability", "capacity", "estimatedFiles"):
        document.pop(key, None)
    worth = document.get("worth")
    if worth is not None:
        document["worth"] = {"recommendation": worth["recommendation"], "reason": worth["costOfNotFixing"],
                             "direction": worth["direction"], "reevaluateWhen": worth.get("reevaluateWhen")}
    document.update({"treatment": None, "taskType": None, "sizeTier": None, "estimate": None})
    _drop_removed_labels(document)
    return document


REMOVED_LABELS = frozenset({"prioritize"})  # 已删去的 Issue 标签(按优先分置顶)


def _drop_removed_labels(document: Document) -> None:
    if "labels" in document:
        document["labels"] = [label for label in document["labels"] or [] if label not in REMOVED_LABELS]


@REGISTRY.upgrader("handoff/outputs/issue.schema.json", 1)
def _issue_v1(document: Document) -> Document:
    document.pop("priorityScore", None)
    document["treatment"] = None
    _drop_removed_labels(document)
    return document


@REGISTRY.upgrader("handoff/outputs/loop.schema.json", 1)
def _loop_v1(document: Document) -> Document:
    for item in document.get("waiting") or []:
        item.pop("priorityScore", None)
        item.update({"treatment": None, "severity": None})
    return document


@REGISTRY.upgrader("handoff/envelope.schema.json", 1)
def _envelope_v1(document: Document) -> Document:
    name = f"handoff/outputs/{document.get('stage')}.schema.json"
    if document.get("status") != "failed" and name in REGISTRY.names() and REGISTRY.current(name) == 2:
        document["outputs"] = REGISTRY.upgrade(name, document["outputs"], 1)
    document["schemaVersion"] = 2
    return document

