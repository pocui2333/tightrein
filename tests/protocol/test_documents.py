"""给人看的三种文档：从 store 与 handoff 渲染，语言按项目配置，自由文字下移标题并闭合代码块。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tightrein.protocol import documents, handoff
from tightrein.protocol.handoff import Handoff, Metrics, Status, Tokens
from tightrein.protocol.naming import FileName, FixedClock, format_iso
from tightrein.store.db import open_database
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.tables import issues

NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)
FENCE = "`" * 3


@dataclass
class FakeRuntime:
    workspace: WorkspaceLayout
    conn: sqlite3.Connection
    clock: FixedClock
    language: str = "zh"


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[FakeRuntime]:
    clock = FixedClock(NOW)
    layout = WorkspaceLayout(tmp_path / "workspaces" / "demo")
    conn = open_database(layout.database, clock=clock)
    issues.save(conn, issues.Issue(id="0018", status="implementing", title="订单列表 | 只显示当天", kind="bug",
                                   origin="problem", branch="fix/18-orders", pr=42, merge_commit="abc123"), clock)
    yield FakeRuntime(layout, conn, clock)
    conn.close()


def _handoff(runtime: FakeRuntime, point: str, facts: dict, *, round: int | None = None, seconds: int = 0,
             tokens: Tokens | None = None, cost: float | None = None) -> None:
    at = format_iso(NOW.replace(second=seconds))
    item = Handoff(point=point, subject="0018", run="R-20261008T030000Z-implement", status=Status.PASSED,
                   summary=f"{point} 完成", facts=facts, round=round, created_at=at,
                   metrics=Metrics(duration_ms=60_000, calls=1, tokens=tokens, cost_usd=cost, cost_estimated=True))
    path = runtime.workspace.step_file("0018", FileName(point, "handoff", "json", round=round))
    handoff.write(path, item)


def test_free_text_demotes_headings_and_closes_fences() -> None:
    text = f"# 原因\n\n{FENCE}python\n# 不是标题\nprint(1)"
    rendered = documents.free_text(text)
    assert rendered.startswith("### 原因")
    assert "# 不是标题" in rendered and "### 不是标题" not in rendered
    assert rendered.endswith(f"print(1)\n{FENCE}")


def test_close_fences_follows_commonmark() -> None:
    assert documents.close_fences(f"{FENCE}\na\n{FENCE}") == f"{FENCE}\na\n{FENCE}"
    # 更短的、换了字符的、带信息串的都不算闭合
    assert documents.close_fences("````\na\n```\n~~~~\n``` python").endswith("\n````")
    assert documents.close_fences("~~~\na") == "~~~\na\n~~~"


def test_pending_is_written_in_the_project_language(runtime: FakeRuntime) -> None:
    path = documents.pending(runtime, "0018", point="implement.approve", decision="确认方案：改查询条件",
                             options=["通过", "不通过并说明原因"], recommendation="通过", reason="# 改动小\n只改一处",
                             if_not="Issue 停在定案", command="tightrein approve 0018")
    assert path.name == "90-issue-pending.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# 待审核：0018 订单列表 \\| 只显示当天")
    assert "| 命令 | `tightrein approve 0018` |" in text
    assert "1. 通过\n2. 不通过并说明原因" in text
    assert "### 改动小" in text and text.endswith("\n") and not text.endswith("\n\n")


def test_failure_lists_what_was_tried(runtime: FakeRuntime) -> None:
    runtime.language = "en"
    path = documents.failure(runtime, "0018", point="implement.check", reason="no progress", tried=["round 1", "round 2"],
                             advice="narrow the plan", command="tightrein approve 0018 --note \"…\"")
    text = path.read_text(encoding="utf-8")
    assert path.name == "90-issue-failure.md"
    assert text.startswith("# Stopped: 0018") and "## Already tried\n\n- round 1\n- round 2" in text


def test_deliver_sums_every_step(runtime: FakeRuntime) -> None:
    _handoff(runtime, "implement.design", {"summary": "查询加上日期条件"}, seconds=1)
    _handoff(runtime, "implement.code", {"changedFiles": [{"path": "src/orders.py", "added": 3, "deleted": 1}]},
             round=1, seconds=2, tokens=Tokens(input=1000, output=200, cache_read=500), cost=0.5)
    _handoff(runtime, "implement.code", {"changedFiles": [{"path": "src/orders.py", "added": 4, "deleted": 1}]},
             round=2, seconds=3, tokens=Tokens(input=2000, output=100), cost=0.25)
    text = documents.deliver(runtime, "0018").read_text(encoding="utf-8")
    assert "查询加上日期条件" in text
    assert "| PR | #42 |" in text and "| 合并提交 | `abc123` |" in text
    assert "| `src/orders.py` | +4 -1 |" in text  # 取最后一轮
    assert "$0.75(估算)" in text and "3k / 300 / 500" in text and "| 轮数 | 2 |" in text
    assert "| 总耗时 | 3m |" in text
