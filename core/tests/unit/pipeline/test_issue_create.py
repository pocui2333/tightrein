import subprocess

from pipeline_world import make_signal
from triage_world import make_triage_world, store_triaged, triage_outputs

from tightrein.domain.enums import HandoffStatus, IssueStatus, ScoreResult, Severity
from tightrein.observability.notify import Notifier
from tightrein.observability.redact import Redactor
from tightrein.pipeline.issue.service import IssueDeps, IssueService
from tightrein.store.files import handoff_files, issue_files
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import issues, problems, scores


class Commands:
    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")


def service(world, layout=None, notifier=None, snapshot="worktree"):
    return IssueService(IssueDeps(layout or world.layout, world.config, world.conn, world.clock, world.events,
                                  notifier=notifier, snapshot=world.worktree if snapshot == "worktree" else snapshot))


def test_create_writes_the_issue_file_index_scores_and_handoff(tmp_path):
    world = make_triage_world(tmp_path)
    store_triaged(world)
    run = service(world).create()
    item = run.items[0]
    assert (item.status, item.issue_id, item.action) == (HandoffStatus.OK, "0001", "created")
    record = issues.get(world.conn, "0001")
    assert record.path == "issues/0001-api-order-500.md"
    document = issue_files.read(world.layout.root / record.path)
    issue = document.issue
    assert (issue.status, issue.severity, issue.problems, issue.root_cause, issue.findings) == (
        IssueStatus.NEEDS_DECISION, Severity.P2, ("P-0001",), ("src/Services/OrderService.src:12",),
        "data/findings/P-0001.md")
    criteria = "- [ ] api-fuzz 对 `GET /api/Order/{id}` 的 `not_a_server_error` 检查以 `Admin` 身份通过"
    assert f"### 验收标准\n\n- [ ] 复现测试在修复前失败、修复后通过" in document.body and criteria in document.body
    assert issue.source == "api-fuzz"
    assert problems.get(world.conn, "P-0001").issue_id == "0001"
    assert {item.item: item.result for item in scores.find(world.conn, run_id=run.run.id)} == {
        "issue.sections": ScoreResult.PASS, "issue.evidence-location": ScoreResult.PASS,
        "issue.absolute-dates": ScoreResult.PASS}
    outputs = handoff_files.read(item.handoff)["outputs"]
    assert (outputs["path"], outputs["notified"], outputs["acceptance"][-1]) == (
        record.path, False, "本 Issue 的复现检查在修复后通过")
    assert service(world).create().items == []


def test_a_problem_with_the_same_root_cause_is_appended_and_raises_the_severity(tmp_path):
    world = make_triage_world(tmp_path)
    store_triaged(world)
    service(world).create()
    store_triaged(world, "P-0002", make_signal(2), outputs=triage_outputs("P-0002", severity="P1"))
    item = service(world).create().items[0]
    assert (item.issue_id, item.action) == ("0001", "appended")
    document = issue_files.read(world.layout.root / issues.get(world.conn, "0001").path)
    assert (document.issue.problems, document.issue.severity) == (("P-0001", "P-0002"), Severity.P1)
    assert "- `P-0002` " in document.body.split("## 引用")[1].split("## 历史")[0]
    assert "追加问题 P-0002(运行" in document.body and "严重度由 P2 提升为 P1" in document.body
    assert problems.get(world.conn, "P-0002").issue_id == "0001"


def test_p0_issues_notify_once_a_day(tmp_path):
    world = make_triage_world(tmp_path)
    commands = Commands()
    notifier = Notifier(world.conn, "macos", world.clock, Redactor(), run=commands)
    store_triaged(world, outputs=triage_outputs(severity="P0"))
    first = service(world, notifier=notifier).create().items[0]
    store_triaged(world, "P-0002", make_signal(2), outputs=triage_outputs("P-0002", severity="P0"))
    second = service(world, notifier=notifier).create().items[0]
    assert [handoff_files.read(item.handoff)["outputs"]["notified"] for item in (first, second)] == [True, False]
    assert len(commands.calls) == 1


def test_a_document_that_fails_the_checks_is_kept_and_the_handoff_fails(tmp_path):
    world = make_triage_world(tmp_path)
    outputs = triage_outputs()
    outputs["evidence"] = {**outputs["evidence"], "facts": [{"location": "src/Services/OrderService.src:12",
                                                             "observation": "最近改过"}]}
    store_triaged(world, outputs=outputs)
    item = service(world).create().items[0]
    assert item.status is HandoffStatus.FAILED and item.path.is_file()
    assert "[issue.absolute-dates]" in item.reason


def test_bare_file_names_are_completed_and_unknown_locations_block_the_issue(tmp_path):
    world = make_triage_world(tmp_path)
    outputs = triage_outputs()
    outputs["evidence"] = {**outputs["evidence"], "facts": [
        {"location": "OrderService.src:12", "observation": "与 `OrderService.src:3` 一致"}]}
    store_triaged(world, outputs=outputs)
    item = service(world).create().items[0]
    assert item.status is HandoffStatus.OK
    text = issue_files.read(world.layout.root / issues.get(world.conn, "0001").path).body
    assert "- `src/Services/OrderService.src:12` 与 `src/Services/OrderService.src:3` 一致" in text
    broken = triage_outputs("P-0002")
    broken["evidence"] = {**broken["evidence"], "facts": [{"location": "src/Services/OrderService.src:99",
                                                           "observation": "见 rule_extractor.src:5"}]}
    broken["rootCauses"] = [{"file": "src/Other.src", "line": 1, "symbol": None}]
    store_triaged(world, "P-0002", make_signal(2), outputs=broken)
    blocked = service(world).create().items[0]
    assert blocked.status is HandoffStatus.FAILED and blocked.issue_id is None and blocked.path is None
    assert "OrderService.src 只有" in blocked.reason
    assert "retriage P-0002" in handoff_files.read(blocked.handoff)["nextAction"]
    assert issues.get(world.conn, "0002") is None and problems.get(world.conn, "P-0002").issue_id is None


def test_output_mode_writes_the_issue_to_the_output_directory_only(tmp_path):
    world = make_triage_world(tmp_path)
    store_triaged(world)
    output = tmp_path / "sandbox"
    item = service(world, layout=WorkspaceLayout(world.layout.root, output)).create().items[0]
    assert item.path == output / "issues" / "0001-api-order-500.md" and item.path.is_file()
    assert item.handoff.parent == output / "handoff"
    assert issues.find(world.conn) == [] and problems.get(world.conn, "P-0001").issue_id is None
    assert handoff_files.read(item.handoff)["outputs"]["path"] == "issues/0001-api-order-500.md"


def test_selected_problems_without_a_create_issue_disposition_are_skipped(tmp_path):
    world = make_triage_world(tmp_path)
    store_triaged(world, outputs=triage_outputs(disposition="deferred"))
    run = service(world).create(("P-0001", "P-0009"))
    assert run.skipped == [("P-0001", "P-0001 最近一次分诊的去向不是提 Issue"), ("P-0009", "P-0009 不存在")]
    store_triaged(world, "P-0002", make_signal(2))
    assert service(world).create(dry_run=True).plan == [("P-0002", "新建")]
