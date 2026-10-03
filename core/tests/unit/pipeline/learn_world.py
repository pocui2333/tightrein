"""learn 各测试共用：工作区与样例数据的写入函数。时区固定为 UTC+9，本周为 2026-10-05(周一)到 2026-10-12。"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from pipeline_world import NOW, make_signal, make_world
from store_problem import save_problem
from triage_world import FakeRunner

from tightrein.contracts import versions
from tightrein.domain.clock import FixedClock
from tightrein.domain.enums import (
    CloseReason,
    Disposition,
    IssueEvent,
    IssueOrigin,
    IssueStatus,
    RunStage,
    RunStatus,
    Severity,
    Verdict,
)
from tightrein.domain.issue import Issue
from tightrein.domain.run import Coverage, EnvironmentDetail, Run
from tightrein.domain.triage import TriageResult
from tightrein.observability.tracing import Tracer
from tightrein.retrieval.service import KnowledgeService
from tightrein.store.files import handoff_files
from tightrein.store.files.layout import ToolLayout
from tightrein.store.repos import issue_events, issues, problem_events, problems, runs, signals, triage
from tightrein.store.repos.issue_events import IssueEventRecord
from tightrein.store.repos.issues import IssueRecord
from tightrein.store.repos.problem_events import ProblemEventRecord
from tightrein.store.repos.triage import TriageRecord

ZONE = timezone(timedelta(hours=9))
WEEK = date(2026, 10, 5)
WEEK_START = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)
LEARN_RUN = "R-20261005-030000-learn"
TRIAGE_RUN = "R-20261001-030000-triage"


@dataclass
class LearnWorld:
    base: object
    tool: ToolLayout = field(default_factory=ToolLayout)
    runner: FakeRunner = field(default_factory=FakeRunner)

    def __getattr__(self, name):
        return getattr(self.base, name)

    def knowledge_service(self):
        return KnowledgeService(self.layout, self.conn, self.clock, Tracer(self.events, self.clock, run_id=LEARN_RUN),
                                zone=ZONE)


def make_learn_world(tmp_path, **config_changes):
    return LearnWorld(make_world(tmp_path, **config_changes))


def save_run(world, run_id, stage=RunStage.COLLECT, probe=None, started=NOW, status=RunStatus.OK, coverage=None,
             ended=None, commit=None, level=None, failed_roles=()):
    run = Run(run_id, stage, started, status, probe=probe, level=level, ended_at=ended, target_commit=commit,
              coverage=coverage or Coverage(), environment_detail=EnvironmentDetail(failed_roles=tuple(failed_roles)))
    runs.save(world.conn, run)
    return run


def save_signal(world, number, run_id, at=NOW, probe=None, **changes):
    if runs.get(world.conn, run_id) is None:
        save_run(world, run_id, probe=probe or make_signal().probe)
    signal = make_signal(number, run_id=run_id, probe=probe, occurred_at=at, **changes)
    signals.save(world.conn, signal)
    return signal


def problem_with(world, problem_id, *signal_list, **changes):
    fingerprint = changes.pop("fingerprint", f"{problem_id.lower()}-fingerprint")
    problem = save_problem(world.conn, problem_id, fingerprint, **changes)
    problems.add_signals(world.conn, problem_id, [signal.id for signal in signal_list])
    return problem


def problem_event(world, problem_id, event, at=NOW, to_status=None, run_id=None, operation="auto"):
    problem_events.append(world.conn, ProblemEventRecord(problem_id, at, event, operation, to_status=to_status,
                                                         run_id=run_id))


def triaged(world, problem_id, attempt=1, verdict=Verdict.CONFIRMED, disposition=Disposition.CREATE_ISSUE,
            at=NOW, run_id=TRIAGE_RUN, outcome=None, outcome_at=None, severity=Severity.P1, refuter=None):
    result = TriageResult(problem_id, attempt, verdict, disposition, "理由", "c" * 40, severity=severity,
                          outcome=outcome, refuter_verdict=refuter)
    triage.save(world.conn, TriageRecord(result, run_id, at, outcome_at))


def issue(world, issue_id="0007", status=None, close_reason=None, problems_=("P-0001",), created=NOW,
          root_cause=(), origin=IssueOrigin.TRIAGE, phase=None):
    if status is None:
        status = IssueStatus.DONE if close_reason in (None, CloseReason.FIXED) else IssueStatus.CANCELLED
    if status is IssueStatus.DONE and close_reason is None:
        close_reason = CloseReason.FIXED
    found = Issue(id=issue_id, slug="order-500", title="订单查询返回 500", status=status, severity=Severity.P1,
                  created_at=created, updated_at=created, problems=tuple(problems_), close_reason=close_reason,
                  root_cause=tuple(root_cause), origin=origin, phase=phase)
    issues.save(world.conn, IssueRecord(found, f"issues/{issue_id}-order-500.md", "0" * 64))
    return found


def issue_event(world, issue_id, event, at=NOW, to_status=None, close_reason=None):
    issue_events.append(world.conn, IssueEventRecord(issue_id, at, event.value, "user", to_status=to_status,
                                                     close_reason=close_reason))


def closed_fixed(world, issue_id, at=NOW):
    issue_event(world, issue_id, IssueEvent.PR_MERGED, at, IssueStatus.DONE, CloseReason.FIXED)


def save_handoff(world, stage, subject_id, outputs, run_id, at=NOW, phase=None, attempt=1, status="ok"):
    document = {"schemaVersion": versions.current("handoff/envelope.schema.json"), "runId": run_id, "stage": stage.value,
                "subject": {"type": "issue", "id": subject_id}, "status": status, "inputsRef": {},
                "outputs": outputs, "nextAction": "无", "createdAt": at.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if status != "ok":
        document["blockedReason"] = "测试"
    return handoff_files.write(world.layout, document, FixedClock(at), conn=world.conn, phase=phase,
                               attempt=attempt).path


def fix_outputs(issue_id="0007", changed=(("src/a.src", 3, 1),), rounds=()):
    return {"issueId": issue_id, "branch": "cty/fix-order-500", "worktree": "worktrees/fix-0007",
            "baseCommit": "d6f37025", "changedFiles": [{"path": path, "added": added, "removed": removed}
                                                        for path, added, removed in changed],
            "rounds": list(rounds)}


def verify_outputs(issue_id="0007", conclusion="passed", items=()):
    return {"issueId": issue_id, "phase": "local", "target": {"url": "http://localhost:5100", "mode": "api"},
            "commit": "e5f6a7b8", "baseCommit": "d6f37025", "items": list(items), "conclusion": conclusion,
            "unverified": []}



def learn_env(world, output_dir=None):
    from tightrein.pipeline.learn.prompts.common import LearnCalls, LearnEnv, LearnPrompt
    from tightrein.store.files.layout import WorkspaceLayout

    layout = world.layout if output_dir is None else WorkspaceLayout(world.layout.root, output_dir)
    prompt = LearnPrompt(world.tool, world.config, LEARN_RUN, layout.knowledge_dir())
    calls = LearnCalls(world.runner, world.clock, prompt, conn=None if output_dir else world.conn)
    return LearnEnv(world.conn, layout, world.config, world.clock, ZONE, calls, world.knowledge_service())


def lesson_output(kind="triage-lesson", slug="timeout-upstream", title="超时报错先查上游依赖", draft=True):
    body = {"type": kind, "slug": slug, "title": title, "summary": "日志中的超时报错", "tags": ["stage:triage"],
            "body": "## 现象\n\n超时报错。\n\n## 为什么会判错\n\n没有查上游。\n\n## 以后怎么判断\n\n先查上游。\n",
            "related": []}
    return {"mode": "lesson", "draft": body if draft else None, "contradictions": [], "duplicates": []}
