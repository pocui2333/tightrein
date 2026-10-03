"""拆分出的后续子任务(redesign/05-fix.md 第 3、4 步)：预估改动超出单个 PR 的上限 thresholds.change 时(C 通道总是如此)，
计划只描述第一个子任务，其余按先后顺序写在 split.followUps 中。计划确认后(用户确认或自动确认)为每个后续子任务建一个
Issue：与用户需求的 Issue 相同(不关联问题、创建即待修)，头信息带父 Issue 的任务类型与按预估定的规模档，从第 0 步分流；
parent 为父 Issue，dependsOn 为前一个子任务的 Issue，前一个合并后才可开始(GitHub 镜像中为子 Issue 与阻塞关系)；
正文写明父 Issue 与顺序。
同一 Issue 只建一次(幂等键 split:<Issue 编号>)，之后重出的计划再带拆分时只在历史中记一行。
waiting_on 是先后顺序的唯一判断：fix prepare、fix start、无人值守推进与运行摘要都用它。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from tightrein.domain import issue_sections
from tightrein.domain.enums import TaskType
from tightrein.domain.issue import Issue, dependency_met
from tightrein.orchestrator.policy import lanes
from tightrein.pipeline.issue.steps import create, transitions
from tightrein.pipeline.issue.steps.transitions import IssueEnv
from tightrein.store import idempotency
from tightrein.store.repos import issues
from tightrein.store.repos.issues import IssueRecord

KEY = "split:{issue_id}"


def waiting_on(conn: sqlite3.Connection, issue: Issue) -> str | None:
    """拆分出的后续子任务在前一个子任务合并之前不能开始，返回原因；没有依赖或依赖已满足时为 None。"""
    if issue.depends_on is None:
        return None
    found = issues.get(conn, issue.depends_on)
    if found is not None and dependency_met(found.issue):
        return None
    state = "不存在" if found is None else f"当前为「{found.issue.status.label}」"
    return f"Issue {issue.id} 排在 Issue {issue.depends_on} 之后，Issue {issue.depends_on} {state}，合并后才可开始"


def follow_ups(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """计划中排在第一个子任务之后的子任务，按先后顺序；没有拆分时为空。"""
    return list((plan.get("split") or {}).get("followUps") or [])


def requirement(parent: Issue, number: int, total: int, previous: str, item: Mapping[str, Any],
                language: str) -> str:
    estimate = item["estimate"]
    lines = [
        f"拆分自 Issue {parent.id}「{parent.title}」的第 {number}/{total} 个子任务；排在 Issue {previous} 之后，"
        f"Issue {previous} 合并后才可开始。",
        "",
        f"目标：{item['goal']}",
        "",
        f"预估改动 {estimate['files']} 个文件、{estimate['lines']} 行；涉及文件：{'、'.join(item['files'])}",
        "",
        f"## {issue_sections.heading(issue_sections.ACCEPTANCE, language)}",
        "",
        *(f"- {criterion}" for criterion in item["acceptance"]),
    ]
    return "\n".join(lines)


def create_follow_ups(env: IssueEnv, issue_id: str, plan: Mapping[str, Any],
                      task_type: TaskType | None = None) -> list[IssueRecord]:
    """计划带拆分时建后续 Issue(子 Issue 带任务类型与按预估定的规模档，从第 0 步分流)，并在当前 Issue 的历史与 GitHub
    镜像评论中写明；返回新建的 Issue。"""
    items = follow_ups(plan)
    if not items:
        return []
    key = KEY.format(issue_id=issue_id)
    existing = idempotency.get(env.conn, key)
    if existing is not None:
        known = "、".join((existing.result or {}).get("issues", [])) or "(上次未建完)"
        transitions.annotate(env, issue_id, f"新计划又拆分出 {len(items)} 个后续子任务；已有后续 Issue {known}，"
                                            "不再新建，需要调整时编辑或关闭这些 Issue")
        return []
    parent = transitions.record_of(env, issue_id).issue
    task_type = task_type or parent.task_type
    total = len(items) + 1
    idempotency.begin(env.conn, key, env.clock)
    created: list[IssueRecord] = []
    previous = issue_id
    for number, item in enumerate(items, start=2):
        record = create.create_manual(
            env.conn, env.layout, env.clock, title=item["title"],
            requirement=requirement(parent, number, total, previous, item, env.config.language),
            severity=parent.severity, slug_max_length=env.config.whole_threshold("issue.slugMaxLength"),
            language=env.config.language, zone=env.zone, depends_on=previous,
            source=f"拆分自 Issue {issue_id} 的第 {number}/{total} 个子任务", task_type=task_type,
            size_tier=lanes.tier_of(env.config, item["estimate"]["files"], item["estimate"]["lines"]),
            parent=issue_id, repro_test=lanes.writes_repro_test(env.config, task_type))
        created.append(record)
        previous = record.issue.id
    idempotency.complete(env.conn, key, {"issues": [record.issue.id for record in created]}, env.clock)
    text = summary(issue_id, plan["split"]["reason"], [record.issue.id for record in created])
    transitions.annotate(env, issue_id, text)
    return created


def summary(issue_id: str, reason: str, created: Sequence[str]) -> str:
    total = len(created) + 1
    order = "、".join(f"{item}(第 {number} 个)" for number, item in enumerate(created, start=2))
    return (f"修复计划拆分为 {total} 个子任务(原因：{reason})：本 Issue {issue_id} 只做第 1 个；后续 Issue {order} "
            "依次在前一个合并后开始")
