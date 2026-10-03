"""pipeline 各测试共用的基础：临时工作区、已迁移的数据库、校验通过的配置、固定时钟与外部依赖的替身。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tightrein.config import project
from tightrein.domain.clock import FixedClock
from tightrein.observability.events import EventLog
from tightrein.observability.redact import Redactor
from tightrein.sources.common.http import HttpResponse
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.migrations.runner import open_database
from tightrein.pipeline.common.deploys import DeployRecord
from tightrein.vcs.errors import RefNotFound

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
RELEASE = "a" * 40
MAIN = "b" * 40

PROJECT = {
    "project": {"name": "demo", "repo": "/tmp/demo-repo", "mainBranch": "main", "language": "zh"},
    "target": {"baseUrl": "https://staging.example.test", "healthcheck": "/health"},
    "accounts": {"roles": {"Admin": {"keychain": "tightrein.demo.admin"}},
                 "login": {"endpoint": "/login", "bodyTemplate": {}, "tokenPath": "token"}},
    "stages": {"collect": {"budgetPerDay": 2}},
    "evaluation": {"judge": {"runner": "claude"}, "budgetUsd": 5},
    "thresholds": {
        "suppressionDays": {"value": 30, "min": 7, "max": 90},
        "triage": {"deferredReopenOccurrences": {"value": 3, "min": 1, "max": 10}},
    },
}


def make_config(tmp_path, **changes):
    """changes 中的顶层键整体替换，值为 None 的键去掉。"""
    data = {key: value for key, value in {**PROJECT, **changes}.items() if value is not None}
    return project.parse(data, tmp_path / "project.yaml")


class FakeDeployments:
    """部署来源的替身：run 为最近一次成功部署，没有时视为没有配置部署来源。"""

    def __init__(self, run=None):
        self.run = run

    def latest_success(self):
        return self.run

    def configured(self):
        return self.run is not None


def deployment_run(sha=RELEASE, run_id=11):
    return DeployRecord(str(run_id), sha, "succeeded", None, "https://ci.example.test/runs/11",
                        datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc))


class FakeRefs:
    def __init__(self, refs=None):
        self.refs = dict(refs or {})

    def rev_parse(self, repo, ref):
        key = (Path(repo).name, ref)
        if key not in self.refs:
            raise RefNotFound(f"{ref} 不存在")
        return self.refs[key]


class FakeTransport:
    def __init__(self, status=200, elapsed_ms=12):
        self.status = status
        self.elapsed_ms = elapsed_ms
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return HttpResponse(self.status, elapsed_ms=self.elapsed_ms, error=None if self.status else "timeout")


class CountingRandom:
    def __init__(self):
        self.count = 0

    def __call__(self, size):
        self.count += 1
        return self.count.to_bytes(size, "big")


@dataclass
class World:
    root: Path
    layout: WorkspaceLayout
    conn: object
    clock: FixedClock
    config: object
    events: EventLog
    redactor: ProbeRedactor = field(default_factory=ProbeRedactor)


def make_world(tmp_path, output_dir=None, **config_changes):
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    clock = FixedClock(NOW)
    layout = WorkspaceLayout(root, output_dir)
    conn = open_database(WorkspaceLayout(root).database(), clock)
    return World(root, layout, conn, clock, make_config(tmp_path, **config_changes), EventLog(layout, Redactor()))


RUN_ID = "R-20261005-030000-collect-api-fuzz"


def make_signal(number=1, run_id=RUN_ID, probe=None, **changes):
    from tightrein.domain.enums import Probe, Source
    from tightrein.domain.signal import Signal

    values = dict(
        id=f"S-01J9Z3{number:020d}", run_id=run_id, source=Source.SYNTHETIC, probe=probe or Probe.API_FUZZ,
        check="not_a_server_error", environment="staging", occurred_at=NOW, release=RELEASE,
        location="GET /api/Order/42", message="500 Internal Server Error",
        context={"response": {"status": 500}}, actor={"role": "Admin"},
    )
    values.update(changes)
    return Signal(**values)


def save_issue(conn, issue_id="0007", status=None, close_reason=None, problems=(), phase=None):
    from tightrein.domain.enums import CloseReason, IssuePhase, IssueStatus, Severity
    from tightrein.domain.issue import Issue
    from tightrein.store.repos import issues
    from tightrein.store.repos.issues import IssueRecord

    if status is None:
        status = IssueStatus.DONE if close_reason in (None, CloseReason.FIXED) else IssueStatus.CANCELLED
    if status is IssueStatus.DONE and close_reason is None:
        close_reason = CloseReason.FIXED
    if status is IssueStatus.IN_PROGRESS and phase is None:
        phase = IssuePhase.FIX
    issue = Issue(id=issue_id, slug="order-500", title="订单查询返回 500", status=status, severity=Severity.P1,
                  created_at=NOW, updated_at=NOW, problems=tuple(problems), close_reason=close_reason, phase=phase)
    issues.save(conn, IssueRecord(issue, f"issues/{issue_id}-order-500.md", "0" * 64))
    return issue


def write_issue_file(world, issue_id="0007", problems=("P-0001",), body="## 描述\n\n订单查询返回 500。\n"):
    """写入已完成(已修复)的 Issue 文件与索引。"""
    from tightrein.domain.enums import CloseReason, IssueStatus, Severity
    from tightrein.domain.issue import Issue
    from tightrein.store.files import issue_files
    from tightrein.store.files.issue_files import IssueDocument

    issue = Issue(id=issue_id, slug="order-500", title="订单查询返回 500", status=IssueStatus.DONE,
                  severity=Severity.P1, created_at=NOW, updated_at=NOW, problems=tuple(problems),
                  close_reason=CloseReason.FIXED)
    return issue_files.write(world.conn, world.layout, IssueDocument(issue, body))
