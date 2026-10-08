"""评估测试共用：真实数据库与缺省配置，代码快照是临时目录里的几个文件；不联网、不调用模型。

模型调用(assess/prompts/common.call)由 `agent` 换成按调用点排队的录制输出；只读 worktree(assess.readonly)由
`readonly` 换成直接指向 repo 夹具的假 Git。
"""

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tightrein.agents.result import CallResult, CallStatus
from tightrein.protocol.git import GitError
from tightrein.protocol.git.git import BlameLine, Commit
from tightrein.protocol.naming import FixedClock
from tightrein.protocol.security import Redactor
from tightrein.settings.load import ProjectFacts, Settings
from tightrein.store.db import open_database
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.tables import occurrences, problems
from tightrein.store.tables.issues import Issue
from tightrein.store.tables.occurrences import Occurrence
from tightrein.store.tables.problems import Problem

NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)
RUN = "R-20261008T030000Z-assess"
COMMIT = "c" * 40
DEFAULTS = Path(__file__).resolve().parents[2] / "settings" / "defaults.json"
SERVICE = """class OrderService:
    def get(self, order_id, user):
        order = self.repo.find(order_id)
        return order


def helper():
    return 1
"""
ROUTE = """from services.orders import OrderService


def show(request, order_id):
    return OrderService().get(order_id, request.user)
"""


def make_settings(*overrides: dict[str, Any], language: str = "zh") -> Settings:
    project = ProjectFacts(repo=None, main_branch="main", language=language)
    return Settings.from_data(json.loads(DEFAULTS.read_text(encoding="utf-8")), *overrides, project=project)


class FakeBreaker:
    def __init__(self) -> None:
        self.failures: dict[str, int] = {}

    def object_failed(self, subject: str) -> bool:
        self.failures[subject] = self.failures.get(subject, 0) + 1
        return self.failures[subject] >= 3

    def object_progressed(self, subject: str) -> None:
        self.failures.pop(subject, None)


class FakeGit:
    """只读 worktree 上的 Git 替身：blame 按 (文件, 行) 查表，log 返回给定的提交。"""

    def __init__(self, repo: Path, *, blames: dict[tuple[str, int], str] | None = None,
                 commits: list[Commit] | None = None, error: GitError | None = None) -> None:
        self.repo = repo
        self.blames = dict(blames or {})
        self.commits = list(commits or [])
        self.error = error
        self.calls: list[tuple[str, Any]] = []

    def blame(self, path: str, start: int, end: int, rev: str = "HEAD") -> list[BlameLine]:
        self.calls.append(("blame", (path, start, rev)))
        if (path, start) not in self.blames:
            raise GitError(f"no such path {path}")
        return [BlameLine(start, self.blames[(path, start)], "zhang", NOW, "")]

    def log(self, rev_range: str, paths: list[str] | None = None, limit: int | None = None) -> list[Commit]:
        self.calls.append(("log", (rev_range, tuple(paths or ()))))
        if self.error is not None:
            raise self.error
        return self.commits


@dataclass
class Agent:
    """按调用点排队的录制输出：dict 为合格的输出，CallStatus 为调用失败；用完即报错(多调了一次)。"""

    queues: dict[str, list[dict[str, Any] | CallStatus]] = field(default_factory=dict)
    calls: list[SimpleNamespace] = field(default_factory=list)

    def queue(self, point: str, *outputs: dict[str, Any] | CallStatus) -> None:
        self.queues.setdefault(point, []).extend(outputs)

    def points(self) -> list[str]:
        return [call.point for call in self.calls]

    def __call__(self, params: Any, context: Any) -> CallResult:
        self.calls.append(params)
        pending = self.queues.get(params.point) or []
        if not pending:
            raise AssertionError(f"没有为 {params.point} 准备输出")
        found = pending.pop(0)
        if isinstance(found, CallStatus):
            return CallResult(status=found, tool="claude", model="opus", error="录制的失败")
        return CallResult(status=CallStatus.OK, tool="claude", model="opus", output=json.loads(json.dumps(found)),
                          duration_ms=10)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def layout(tmp_path: Path) -> WorkspaceLayout:
    return WorkspaceLayout(tmp_path / "workspaces" / "demo")


@pytest.fixture
def conn(layout: WorkspaceLayout, clock: FixedClock) -> Iterator[sqlite3.Connection]:
    connection = open_database(layout.database, clock=clock)
    yield connection
    connection.close()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """只读 worktree 的替身：两个源文件、一个隐藏目录下的文件。"""
    root = tmp_path / "repo"
    (root / "services").mkdir(parents=True)
    (root / "services" / "orders.py").write_text(SERVICE, encoding="utf-8")
    (root / "routes").mkdir()
    (root / "routes" / "orders.py").write_text(ROUTE, encoding="utf-8")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("name: ci\non: push\n", encoding="utf-8")
    return root


@pytest.fixture
def runtime(tmp_path: Path, layout: WorkspaceLayout, conn: sqlite3.Connection, clock: FixedClock) -> SimpleNamespace:
    """Runtime 的替身：只给评估用到的部分。"""
    return SimpleNamespace(
        workspace=layout, settings=make_settings(), conn=conn, clock=clock, run=RUN, language="zh", github=None,
        redactor=Redactor(), tool=SimpleNamespace(root=tmp_path),
        agents=SimpleNamespace(quota=SimpleNamespace(reserve_reached=lambda: False), breaker=FakeBreaker()),
        scope=lambda subject, point: SimpleNamespace(conn=conn, clock=clock, subject=subject, point=point),
    )


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> Agent:
    from tightrein.assess.prompts import common

    recorded = Agent()
    monkeypatch.setattr(common, "call", recorded)
    return recorded


@pytest.fixture
def fake_git(repo: Path) -> FakeGit:
    return FakeGit(repo, blames={("services/orders.py", 3): "a" * 40})


@pytest.fixture
def readonly(monkeypatch: pytest.MonkeyPatch, fake_git: FakeGit) -> FakeGit:
    """只读 worktree 直接是 repo 夹具，取证 commit 固定为 COMMIT(给出 commit 时用给出的)。"""
    from tightrein.assess import assess

    @contextmanager
    def fake(runtime: Any, commit: str | None = None) -> Iterator[tuple[FakeGit, str]]:
        yield fake_git, commit or COMMIT

    monkeypatch.setattr(assess, "readonly", fake)
    return fake_git


@pytest.fixture
def add_occurrence(conn: sqlite3.Connection, clock: FixedClock) -> Callable[..., Occurrence]:
    def add(problem: Problem, *, seen: datetime = NOW, commit: str | None = "abc1234",
            evidence: dict[str, Any] | None = None, flags: dict[str, Any] | None = None,
            location: str | None = None, signal: str | None = None) -> Occurrence:
        where = location if location is not None else problem.location
        item = Occurrence(problem=problem.id, seen_at=seen, source=problem.source, run=RUN, commit=commit,
                          evidence={"signal": signal or f"S-{problem.id}-{seen:%H%M%S}",
                                    "checkType": problem.check_type, "location": where, "message": problem.title,
                                    **(flags or {}), "evidence": dict(evidence or {})})
        item.id = occurrences.add(conn, item, clock)
        return item

    return add


@pytest.fixture
def make_problem(conn: sqlite3.Connection, clock: FixedClock,
                 add_occurrence: Callable[..., Occurrence]) -> Callable[..., Problem]:
    def make(problem_id: str = "P-0001", *, status: str = "new", source: str = "collect.platform_errors",
             check_type: str = "error", title: str = "保存订单时报错", location: str | None = "services/orders.py:3",
             evidence: dict[str, Any] | None = None, flags: dict[str, Any] | None = None, count: int = 1,
             extra: dict[str, Any] | None = None, seen: datetime = NOW, issue: str | None = None) -> Problem:
        problem = Problem(id=problem_id, fingerprint=f"fp-{problem_id}", source=source, check_type=check_type,
                          status=status, title=title, first_seen=seen - timedelta(days=1), last_seen=seen,
                          location=location, count=count, last_commit="abc1234", issue=issue,
                          extra=dict(extra or {}))
        problems.save(conn, problem, clock)
        add_occurrence(problem, seen=seen, evidence=evidence, flags=flags, signal=f"S-{problem_id}")
        return problem

    return make


@pytest.fixture
def make_issue(runtime: SimpleNamespace) -> Callable[..., Issue]:
    """经 issue/files.write 建一个 Issue(记录、正文与索引)。"""
    from tightrein.assess.issue import body, files

    def make(issue_id: str = "0007", *, status: str = "needs_decision", origin: str = "problem",
             severity: str | None = "P2", problems_: list[str] | None = None, extra: dict[str, Any] | None = None,
             stage: str | None = None, step: str | None = None, held_by: str | None = None,
             title: str | None = None) -> Issue:
        record = Issue(id=issue_id, status=status, title=title or f"Issue {issue_id}", kind="bug", origin=origin,
                       severity=severity, stage=stage, step=step, held_by=held_by,
                       extra={"slug": f"issue-{issue_id}", "problems": list(problems_ or []), "history": [],
                              **(extra or {})})
        text = body.document(f"{issue_id} {record.title}", {"problem": "订单查询没有按用户过滤",
                                                            "cause": "`services/orders.py:3`",
                                                            "acceptance": "- [ ] 返回 404"}, "zh")
        files.write(runtime, record, body=text)
        return record

    return make


def confirmed_output(**changes: Any) -> dict[str, Any]:
    """一份通过全部证据检查的取证输出(位置都在 repo 夹具里)。"""
    output: dict[str, Any] = {
        "analysis": "读了 services/orders.py:3 与 routes/orders.py:5，调用链从路由直达服务，没有归属过滤。",
        "verdict": "confirmed",
        "facts": [{"location": "services/orders.py:3", "observation": "按编号查询订单，没有按用户过滤"}],
        "trigger": "登录用户请求他人的订单编号",
        "counterEvidence": [{"check": "路由层有没有归属校验", "entry": "routes/orders.py:4",
                             "upstreamValidation": {"status": "absent", "location": None},
                             "result": "路由直接调用服务，没有校验"}],
        "impact": {"kind": "non-core-error", "roles": ["普通用户"], "data": "订单", "callSites": ["routes/orders.py:5"],
                   "consequence": "看到他人的订单"},
        "sourceOfPhenomenon": None,
        "rootCauses": [{"file": "services/orders.py", "line": 3, "symbol": "OrderService.get"}],
        "fixedOnMain": None, "tradeoffHit": None, "missingInfo": [], "incidental": [],
        "report": {"title": "[订单] 可以读到他人的订单", "summary": "按编号查询订单时没有按用户过滤。",
                   "steps": ["用户 A 登录", "请求用户 B 的订单编号"], "expected": "返回 404", "actual": "返回订单",
                   "acceptance": ["用户 A 请求用户 B 的订单返回 404"], "severity": "P2",
                   "severityReason": "只影响少量订单，可以绕开"},
        "assessment": {"worth": "fix", "worthReason": "不修会泄露订单；修只改一处", "taskType": "bug", "size": "small",
                       "files": [{"path": "services/orders.py", "isNew": False}], "direction": "查询时加用户条件",
                       "flags": {key: {"flagged": False, "reason": None, "locations": []}
                                 for key in ("design", "dataStructure", "publicContract")},
                       "reevaluateWhen": None, "outOfScope": ["其他列表接口"], "mustKeep": ["接口返回结构不变"]},
        "notes": [{"location": "routes/orders.py:4", "description": "订单详情的路由", "role": "related"}],
        "knowledgeSuggestions": [],
    }
    output.update(changes)
    return output


def refuted_output(**changes: Any) -> dict[str, Any]:
    return confirmed_output(**{"verdict": "refuted", "report": None, "assessment": None, "rootCauses": [],
                               "impact": None, "trigger": None,
                               "sourceOfPhenomenon": {"location": "routes/orders.py:4", "factRef": None,
                                                      "explanation": "路由只对本人开放"}, **changes})


def insufficient_output(**changes: Any) -> dict[str, Any]:
    return confirmed_output(**{"verdict": "insufficient", "report": None, "assessment": None, "rootCauses": [],
                               "impact": None, "trigger": None, "counterEvidence": [],
                               "missingInfo": [{"item": "业务量级", "source": "user"}], **changes})


@pytest.fixture
def good_output() -> Callable[..., dict[str, Any]]:
    return confirmed_output


@pytest.fixture
def outputs() -> SimpleNamespace:
    """三种典型的取证输出：confirmed(...)、refuted(...)、insufficient(...)。"""
    return SimpleNamespace(confirmed=confirmed_output, refuted=refuted_output, insufficient=insufficient_output)


@pytest.fixture
def kit() -> SimpleNamespace:
    """测试文件要用的常量与构造函数(importlib 导入模式下测试文件不能直接 import conftest)。"""
    return SimpleNamespace(NOW=NOW, RUN=RUN, COMMIT=COMMIT, FakeGit=FakeGit, make_settings=make_settings)
