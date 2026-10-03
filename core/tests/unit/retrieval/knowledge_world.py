"""retrieval 各测试共用的环境：临时的本工具仓库与工作区、已迁移的数据库、固定时钟，以及写各类知识文件的函数。

条目的 frontmatter 按 data/knowledge.schema.json 写出，正文第一行是一级标题；Issue、发现报告、修复报告与
改进提案按各自的 schema 写出。所有写文件的函数返回文件路径，便于测试改写或删除。
"""

from datetime import datetime, timedelta, timezone

from replay_support import init_tool_repo, replay_runner

from tightrein.domain.clock import FixedClock
from tightrein.domain.enums import KnowledgeType, Probe, ProblemStatus, Source
from tightrein.domain.problem import Problem, ProblemScope
from tightrein.domain.signal import Signal
from tightrein.observability.events import EventLog
from tightrein.observability.redact import Redactor
from tightrein.observability.tracing import Tracer
from tightrein.store.files import markdown
from tightrein.store.files.layout import ToolLayout
from tightrein.store.files.markdown import MarkdownDocument
from tightrein.store.migrations.runner import open_database
from tightrein.store.repos import problems, signals

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
TOKYO = timezone(timedelta(hours=9))
RUN = "R-20261005-030000-learn"


def entry_frontmatter(entry_id, kind, summary, tags, **fields):
    data = {"id": entry_id, "type": kind.value, "summary": summary, "tags": list(tags),
            "status": fields.pop("status", "active"), "supersededBy": fields.pop("superseded_by", None),
            "updated": fields.pop("updated", "2026-09-01"), "reviewBy": fields.pop("review_by", "2027-03-01"),
            "related": list(fields.pop("related", ()))}
    data.update(fields)
    return data


class KnowledgeWorld:
    def __init__(self, tmp_path):
        self.clock = FixedClock(NOW)
        self.tool = ToolLayout(tmp_path / "tightrein")
        self.layout = self.tool.workspace("sample")
        self.layout.root.mkdir(parents=True)
        self.conn = open_database(self.layout.database(), self.clock)
        self.redactor = Redactor()
        self.tracer = Tracer(EventLog(self.layout, self.redactor), self.clock, run_id=RUN, stage="learn")

    def write(self, path, frontmatter, body):
        markdown.write(path, MarkdownDocument(frontmatter, body))
        return path

    def entry(self, entry_id, slug, title, summary, tags=("topic",), body="正文。\n", **fields):
        kind = KnowledgeType.from_prefix(entry_id.split("-")[0])
        path = self.layout.knowledge_file(kind, entry_id, slug)
        return self.write(path, entry_frontmatter(entry_id, kind, summary, tags, **fields), f"# {title}\n\n{body}")

    def issue(self, issue_id, slug, title, root_cause=("src/OrderService.cs:3",), problems=("P-0001",), body="正文\n"):
        data = {"type": "issue", "id": issue_id, "title": title, "status": "fixing", "severity": "P1",
                "problems": list(problems), "rootCause": list(root_cause), "createdAt": "2026-09-20T01:00:00Z",
                "updatedAt": "2026-09-21T01:00:00Z"}
        return self.write(self.layout.issue_file(issue_id, slug), data, f"# {title}\n\n{body}")

    def finding(self, problem_id, title, summary, tags, body="正文\n"):
        data = {"type": "finding", "id": f"triage-{problem_id}", "summary": summary, "tags": list(tags),
                "runId": "R-20260920-010000-triage", "createdAt": "2026-09-20T01:00:00Z",
                "updatedAt": "2026-09-22T01:00:00Z", "problemId": problem_id, "status": "create-issue",
                "verdict": "confirmed", "disposition": "create-issue", "triageCommit": "abc1234"}
        return self.write(self.layout.finding(problem_id), data, f"# {title}\n\n{body}")

    def fix_report(self, issue_id, title, summary, tags, body="正文\n"):
        data = {"type": "fix-report", "id": f"fix-{issue_id}", "summary": summary, "tags": list(tags),
                "runId": "R-20260923-010000-fix", "createdAt": "2026-09-23T01:00:00Z",
                "updatedAt": "2026-09-23T02:00:00Z", "issueId": issue_id, "status": "ok",
                "branch": f"cty/fix-{issue_id}", "baseCommit": "abc1234"}
        return self.write(self.layout.fix_report(issue_id), data, f"# {title}\n\n{body}")

    def problem(self, problem_id, *locations):
        """一个问题及其信号，信号的 location 依次取 locations。"""
        saved = []
        for location in locations:
            number = len(self.conn.execute("SELECT id FROM signals").fetchall()) + 1
            signal = Signal(id=f"S-01J9Z3{number:020d}", run_id="R-20261004-010000-collect-api-fuzz",
                            source=Source.ERROR, probe=Probe.API_FUZZ, check="not_a_server_error",
                            environment="staging", occurred_at=NOW, release="abc1234", location=location,
                            message="500", context={"response": {"status": 500}}, actor={"role": "Company"})
            signals.save(self.conn, signal)
            saved.append(signal.id)
        problems.save(self.conn, Problem(
            id=problem_id, fingerprint=f"{int(problem_id[2:]):016x}", fingerprint_version=1, probe=Probe.API_FUZZ,
            title="问题", status=ProblemStatus.NEW, first_seen_at=NOW, last_seen_at=NOW,
            scope=ProblemScope(locations[0], frozenset({"Company"})),
        ))
        problems.add_signals(self.conn, problem_id, saved)

    def close(self):
        self.conn.close()



class ReplayRig:
    """真实的 Runner 以 replay 执行器运行：本工具仓库是 git 仓库，guards 照常检查。"""

    def __init__(self, world, repos, config, recordings):
        self.world = world
        self.repos = repos
        self.config = config
        self.recordings = recordings
        init_tool_repo(repos, world.tool.root)

    def runner(self):
        world = self.world
        return replay_runner(conn=world.conn, layout=world.layout, tool=world.tool, config=self.config,
                             tracer=world.tracer, redactor=world.redactor, environ=self.repos.environ,
                             recordings=self.recordings)
