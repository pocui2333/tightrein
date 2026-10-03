"""triage、issue 与静态审查器测试共用：假执行器、只读 worktree、知识条目与样例问题。"""

from dataclasses import dataclass, field

from pipeline_world import NOW, make_world
from store_problem import save_problem

from tightrein.domain.enums import KnowledgeType, RunnerStatus
from tightrein.observability.tracing import Tracer
from tightrein.retrieval.service import KnowledgeService
from tightrein.runner.result import RunnerResult, Usage
from tightrein.store.files import markdown
from tightrein.store.files.layout import ToolLayout
from tightrein.store.files.markdown import MarkdownDocument
from tightrein.store.repos import problems, signals

TRIAGE_RUN = "R-20261005-030000-triage"
COMMIT = "c" * 40
SERVICE = "\n".join(f"line {number}" for number in range(1, 41)) + "\n"
CONTROLLER = "\n".join(f"controller {number}" for number in range(1, 21)) + "\n"


class FakeRunner:
    """按角色前缀返回预置结果；同一角色的多个结果依次取用，取完后重复最后一个。"""

    def __init__(self, outputs=None):
        self.outputs = {role: list(items) for role, items in (outputs or {}).items()}
        self.tasks = []

    def add(self, role, *items):
        self.outputs.setdefault(role, []).extend(items)
        return self

    def run(self, task, *, clock, runner_override=None, model_override=None):
        self.tasks.append(task)
        role = next((name for name in sorted(self.outputs, key=len, reverse=True) if task.role.startswith(name)), None)
        if role is None:
            raise AssertionError(f"没有为角色 {task.role} 准备结果")
        queue = self.outputs[role]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, RunnerResult):
            return item
        if isinstance(item, RunnerStatus):
            return RunnerResult(item, "fake", error_type="fake-error", attempts=1)
        return RunnerResult(RunnerStatus.OK, "fake", output=item, attempts=1,
                            usage=Usage(input_tokens=100, output_tokens=20, cost_usd=0.01),
                            transcript_path=f"transcripts/{task.role}-{task.subject_id}.jsonl")

    def roles(self):
        return [task.role for task in self.tasks]


@dataclass
class TriageWorld:
    base: object
    tool: ToolLayout
    runner: FakeRunner = field(default_factory=FakeRunner)

    def __getattr__(self, name):
        return getattr(self.base, name)

    @property
    def worktree(self):
        return self.base.layout.readonly_worktree()

    def knowledge(self, entry_id, slug, summary, body="正文。\n", tags=("topic",)):
        kind = KnowledgeType.from_prefix(entry_id.split("-")[0])
        frontmatter = {"id": entry_id, "type": kind.value, "summary": summary, "tags": list(tags),
                       "status": "active", "supersededBy": None, "updated": "2026-09-01", "reviewBy": "2027-03-01",
                       "related": []}
        markdown.write(self.base.layout.knowledge_file(kind, entry_id, slug),
                       MarkdownDocument(frontmatter, f"# {summary}\n\n{body}"))
        tracer = Tracer(self.base.events, self.base.clock, run_id=TRIAGE_RUN)
        KnowledgeService(self.base.layout, self.base.conn, self.base.clock, tracer).sync()


def make_triage_world(tmp_path, **config_changes):
    base = make_world(tmp_path, **config_changes)
    worktree = base.layout.readonly_worktree()
    (worktree / "src" / "Services").mkdir(parents=True)
    (worktree / "src" / "Controllers").mkdir(parents=True)
    (worktree / "src" / "Services" / "OrderService.src").write_text(SERVICE, encoding="utf-8")
    (worktree / "src" / "Controllers" / "OrderController.src").write_text(CONTROLLER, encoding="utf-8")
    return TriageWorld(base, ToolLayout())


def store_problem(world, problem_id="P-0001", signal=None, **changes):
    """写入问题与它的信号(信号所属的运行按需补上)。"""
    from pipeline_world import make_signal

    from tightrein.domain.enums import RunStage, RunStatus
    from tightrein.domain.run import Run
    from tightrein.store.repos import runs

    found = signal or make_signal()
    if runs.get(world.conn, found.run_id) is None:
        runs.save(world.conn, Run(found.run_id, RunStage.COLLECT, NOW, RunStatus.OK, probe=found.probe))
    signals.save(world.conn, found)
    fingerprint = changes.pop("fingerprint", f"{problem_id.lower()}-fingerprint")
    problem = save_problem(world.conn, problem_id, fingerprint, probe=found.probe, **changes)
    problems.add_signals(world.conn, problem_id, [found.id])
    return problem


def verification(verdict="confirmed", **changes):
    """一份能通过证据检查的 claim-verifier 输出。"""
    output = {
        "analysis": "OrderService.Get 直接按编号查询，控制器入口也没有按公司过滤。",
        "verdict": verdict,
        "facts": [{"location": "src/Services/OrderService.src:12", "observation": "id 直接用于查询，没有按公司过滤"}],
        "trigger": "Admin 以外的角色传入其他公司的订单编号",
        "counterEvidence": [{"check": "控制器入口是否校验归属", "entry": "src/Controllers/OrderController.src:8",
                             "upstreamValidation": {"status": "absent", "location": None}, "result": "入口没有校验"}],
        "impact": {"kind": "non-core-error", "roles": ["Admin"], "data": "订单", "callSites": [],
                   "consequence": "查询失败并返回 500"},
        "sourceOfPhenomenon": None,
        "rootCauses": [{"file": "src/Services/OrderService.src", "line": 12, "symbol": "OrderService.Get"}],
        "fixedOnMain": None,
        "tradeoffHit": None,
        "missingInfo": [],
        "incidental": [],
        "report": None,
        "assessment": assessment(),
    }
    if verdict == "refuted":
        output.update(trigger=None, impact=None, rootCauses=[], assessment=None,
                      sourceOfPhenomenon={"location": "src/Controllers/OrderController.src:5", "factRef": None,
                                          "explanation": "入口已拒绝非法编号"},
                      counterEvidence=[{"check": "入口是否校验", "entry": "src/Controllers/OrderController.src:8",
                                        "upstreamValidation": {"status": "present",
                                                               "location": "src/Controllers/OrderController.src:5"},
                                        "result": "入口已校验"}])
    if verdict == "insufficient":
        output.update(trigger=None, impact=None, rootCauses=[], assessment=None,
                      missingInfo=[{"item": "线上的订单数量", "source": "user"}])
    output.update(changes)
    return output


def assessment(worth="fix", **changes):
    """claim-verifier 输出中的评估：价值判断、任务类型、预估规模与修复方向。"""
    output = {
        "worth": worth,
        "worthReason": "每次查询其他公司的订单都返回 500",
        "taskType": "bug",
        "estimate": {"files": [{"path": "src/Services/OrderService.src", "isNew": False}], "lines": 8},
        "direction": "在 OrderService.Get 中按公司过滤",
        "flags": {"design": {"flagged": False}, "dataStructure": {"flagged": False},
                  "publicContract": {"flagged": False}},
        "reevaluateWhen": "再出现 3 次" if worth == "defer" else None,
        "outOfScope": [],
        "mustKeep": [],
    }
    output.update(changes)
    return output


def triage_outputs(problem_id="P-0001", **changes):
    """一份提 Issue 的分诊交接文档 outputs(符合 handoff/outputs/triage.schema.json)。"""
    found = verification()
    outputs = {
        "problemId": problem_id,
        "claim": {"statement": "Admin 调用 GET /api/Order/{id}，传入 /api/Order/42 时服务端返回 500",
                  "title": "订单查询返回 500",
                  "facts": [{"label": "出现次数", "value": 3},
                            {"label": "信号 S-1", "value": {"context": {"reproduce": "curl -X GET /api/Order/42"}}}],
                  "entryPoints": ["GET /api/Order/{id}"], "userNotes": []},
        "verdict": "confirmed", "severity": "P2", "complexity": "low",
        "rootCauses": found["rootCauses"], "introducedBy": [{"commit": "a" * 40, "author": "zhang", "pr": 185}],
        "disposition": "create-issue", "reason": "判定为确认成立；去向为提 Issue", "triageCommit": COMMIT,
        "refuterVerdict": None, "treatment": "scheduled", "taskType": "bug", "sizeTier": "micro",
        "estimate": assessment()["estimate"], "flags": assessment()["flags"], "labels": [],
        "evidence": {key: found[key] for key in ("facts", "trigger", "counterEvidence", "impact",
                                                 "sourceOfPhenomenon")},
        "worth": {"recommendation": "fix", "reason": assessment()["worthReason"],
                  "direction": assessment()["direction"], "reevaluateWhen": None},
        "scope": {"outOfScope": [], "mustKeep": []},
        "fixedOnMain": None,
        "tradeoffHit": None, "mergedInto": None, "missingInfo": [], "incidentalFindings": [], "scores": [],
        "attempts": [{"role": "claim-verifier", "statuses": ["ok"]}],
    }
    outputs.update(changes)
    return outputs


def store_triaged(world, problem_id="P-0001", signal=None, outputs=None, **changes):
    """写入一个分诊去向为提 Issue 的问题：问题、分诊结论与分诊交接文档。"""
    from tightrein.contracts import versions
    from tightrein.domain.enums import Disposition, ProblemStatus, Severity, Verdict
    from tightrein.domain.triage import TriageResult
    from tightrein.store.files import handoff_files
    from tightrein.store.repos import triage
    from tightrein.store.repos.triage import TriageRecord

    changes.setdefault("status", ProblemStatus.ONGOING)
    problem = store_problem(world, problem_id, signal, **changes)
    found = outputs or triage_outputs(problem_id)
    causes = tuple(root_cause_of(item) for item in found["rootCauses"])
    result = TriageResult(problem_id, 1, Verdict(found["verdict"]), Disposition(found["disposition"]), found["reason"],
                          found["triageCommit"], severity=Severity(found["severity"]), root_causes=causes)
    triage.save(world.conn, TriageRecord(result, TRIAGE_RUN, NOW))
    document = {"schemaVersion": versions.current("handoff/envelope.schema.json"), "runId": TRIAGE_RUN, "stage": "triage",
                "subject": {"type": "problem", "id": problem_id}, "status": "ok", "inputsRef": {}, "outputs": found,
                "nextAction": "交给 issue 创建", "createdAt": "2026-10-05T03:00:00Z"}
    handoff_files.write(world.layout, document, world.clock, conn=world.conn)
    return problem


def root_cause_of(item):
    from tightrein.domain.triage import RootCause

    return RootCause(item["file"], item["line"], item.get("symbol"))
