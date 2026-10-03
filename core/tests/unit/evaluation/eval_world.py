"""evaluation 各测试共用的环境：临时的本工具仓库(git 仓库，工作区在 workspaces/sample)、被测项目仓库、评测用例与封存。

写用例的函数只写文件；seal 按当前文件重算 manifest.json，commit 提交全部改动，使「已封存且已提交」成为测试的起点。
runner 以 replay 执行器构造真实的 Runner，供模型评审的测试回放录制的评审结果。
"""

import json
import sys
from datetime import datetime, timezone

from replay_support import init_tool_repo, replay_runner

from tightrein.domain.clock import FixedClock
from tightrein.evaluation import manifest
from tightrein.observability.events import EventLog
from tightrein.observability.redact import Redactor
from tightrein.observability.tracing import Tracer
from tightrein.store.files.layout import ToolLayout
from tightrein.store.migrations.runner import open_database
from tightrein.vcs.process import VcsProcess

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
RUN = "R-20261005-030000-improve"
SOURCE_RUN = "R-20260920-010000-triage"
EVALUATION = "EV-20261005-030000"
BEHAVIOR_FILE = "fake-module.json"
TRIAGE_OUTPUTS = {
    "problemId": "P-0042", "verdict": "confirmed", "disposition": "create-issue", "reason": "查询缺少公司过滤",
    "claim": {"statement": "订单接口越权", "title": "订单越权", "facts": [], "entryPoints": ["GET /api/Order/{id}"],
              "userNotes": []},
    "triageCommit": "abc1234", "rootCauses": [{"file": "src/OrderService.cs", "line": 3, "symbol": None}],
    "introducedBy": [], "labels": [], "missingInfo": [],
    "incidentalFindings": [], "scores": [], "attempts": [{"role": "claim-verifier", "statuses": ["ok"]}],
    "evidence": {"facts": [{"location": "src/OrderService.cs:3", "observation": "没有按公司过滤"}], "trigger": None,
                 "counterEvidence": [{"check": "入口是否按公司过滤", "entry": "GET /api/Order/{id}",
                                      "upstreamValidation": {"status": "absent", "location": None},
                                      "result": "没有过滤"}],
                 "impact": None, "sourceOfPhenomenon": None},
}
FAKE_MODULE = r'''import json
import sys
import time
from pathlib import Path

args = sys.argv[1:]


def value(flag):
    return args[args.index(flag) + 1]


module = args[0]
output = Path(value("--output"))
workspace = Path(value("--workspace"))
source_file = Path(value("--input"))
case_id = source_file.parent.parent.name
behaviors = json.loads((workspace.parent.parent / "fake-module.json").read_text(encoding="utf-8"))
spec = dict(behaviors.get(case_id, {}))
spec.update(spec.pop("byAttempt", {}).get(output.name, {}))
output.mkdir(parents=True, exist_ok=True)
(output / "argv.json").write_text(json.dumps(args), encoding="utf-8")
time.sleep(spec.get("sleep", 0))
source = json.loads(source_file.read_text(encoding="utf-8"))
subject = source["subject"]["id"]
if not spec.get("noHandoff"):
    status = spec.get("status", "ok")
    handoff = {"schemaVersion": 2, "runId": "R-20261005-040000-" + module, "stage": module,
               "subject": source["subject"], "status": status, "inputsRef": {}, "outputs": spec.get("outputs", {}),
               "nextAction": "无", "blockedReason": None if status == "ok" else "等待用户确认",
               "createdAt": "2026-10-05T04:00:00Z"}
    (output / "handoff").mkdir(exist_ok=True)
    (output / "handoff" / f"{module}-{subject}.json").write_text(json.dumps(handoff, ensure_ascii=False),
                                                                 encoding="utf-8")
runner = spec.get("runner", "ok")
role = "claim-verifier"
if runner is not None:
    directory = output / "raw" / "runner" / f"{role}-{subject}"
    directory.mkdir(parents=True, exist_ok=True)
    result = {"status": runner, "errorType": spec.get("errorType"), "output": None, "tool": "claude"}
    (directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
    event = {"timestamp": "2026-10-05T04:00:00Z", "run_id": "R-20261005-040000-" + module,
             "trace_id": "0af7651916cd43dd8448eb211c80319c", "span_id": "b7ad6b7169203331", "parent_span_id": None,
             "stage": module, "operation": "invoke_agent", "agent": "claude", "model": "claude-opus",
             "input_tokens": 1000, "output_tokens": 100, "cost_usd": spec.get("cost", 0.5), "duration_ms": 5000,
             "status": runner, "attributes": {}}
    (output / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    (output / "transcripts").mkdir(exist_ok=True)
    (output / "transcripts" / f"{role}-{subject}.jsonl").write_text("{}\n", encoding="utf-8")
if "diff" in spec:
    (output / "changes.patch").write_text(spec["diff"], encoding="utf-8")
sys.stderr.write(spec.get("stderr", ""))
sys.exit(spec.get("exit", 0))
'''


def handoff(outputs=None, status="ok", subject_id="P-0042", stage="triage"):
    return {"schemaVersion": 2, "runId": SOURCE_RUN, "stage": stage, "subject": {"type": "problem", "id": subject_id},
            "status": status, "inputsRef": {}, "outputs": outputs or {}, "nextAction": "无",
            "blockedReason": None if status == "ok" else "原因", "createdAt": "2026-09-20T01:00:00Z"}


def case_data(case_id="E-0001", module="triage", commit="abc1234", **changes):
    data = {"schemaVersion": 1, "id": case_id, "kind": "module", "module": module, "title": "越权读取判为成立",
            "category": "representative", "source": {"runId": SOURCE_RUN, "subjectId": "P-0042"},
            "input": {"handoff": "handoff.json", "commit": commit}}
    data.update(changes)
    return data


class EvalWorld:
    """假模块从版本快照根目录的 fake-module.json 读取行为，因此基线与候选可以表现不同。"""

    def __init__(self, tmp_path, repos):
        self.repos = repos
        self.clock = FixedClock(NOW)
        self.tool = ToolLayout(tmp_path / "tightrein")
        self.layout = self.tool.workspace("sample")
        self.layout.root.mkdir(parents=True)
        self.process = VcsProcess(environ=repos.environ)
        self.redactor = Redactor()
        self.tracer = Tracer(EventLog(self.layout, self.redactor), self.clock, run_id=RUN, stage="improve")
        self.conn = open_database(self.layout.database(), self.clock)
        init_tool_repo(repos, self.tool.root)

    def runner(self, config, recordings):
        return replay_runner(conn=self.conn, layout=self.layout, tool=self.tool, config=config, tracer=self.tracer,
                             redactor=self.redactor, environ=self.repos.environ, recordings=recordings)

    def snapshot(self, commit="abc1234", files=None):
        """被测项目在某个 commit 的代码快照，放在评测目录下(不纳入版本管理)。"""
        root = self.layout.eval_project_snapshot(EVALUATION, commit)
        for path, text in (files or {"src/OrderService.cs": "class OrderService\n{\n    int Page = 1;\n}\n"}).items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text, encoding="utf-8")
        return root

    def fake_module(self, directory):
        """写出扮演被测模块的脚本，返回启动它的命令。"""
        script = directory / "fake_module.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(FAKE_MODULE, encoding="utf-8")
        return [sys.executable, str(script)]

    def behave(self, root, behaviors):
        """假模块在该版本中的行为：用例编号到交接文档状态、outputs、执行器状态、费用等。"""
        return self.write_json(root / BEHAVIOR_FILE, behaviors)

    def write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def module_case(self, case_id="E-0001", module="triage", input_handoff=None, **changes):
        directory = self.layout.evals_dir() / module / case_id
        self.write_json(directory / "case.json", case_data(case_id, module, **changes))
        self.write_json(directory / "input" / "handoff.json", input_handoff or handoff())
        return directory

    def retrieval_cases(self, *cases):
        path = self.layout.retrieval_cases()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases), encoding="utf-8")
        return path

    def seal(self):
        cases = manifest.compute(self.layout)
        self.write_json(self.layout.eval_manifest(), {"schemaVersion": 1, "cases": cases})

    def commit(self, message="test: 评测用例"):
        self.repos.commit(self.tool.root, message)

    def sealed(self):
        self.seal()
        self.commit()
