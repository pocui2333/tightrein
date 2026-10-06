import io
import json

from cli_world import make_cli_world
from triage_world import make_triage_world, store_problem, store_triaged

from tightrein.cli import exit_codes
from tightrein.cli.commands.issue import _mirror_lines
from tightrein.cli.main import main
from tightrein.extensions.invoke import ProcessOutcome
from tightrein.pipeline.common.deploys import UNCONFIGURED as DEPLOY_UNCONFIGURED
from tightrein.pipeline.issue.steps.github import MirrorReport
from tightrein.sources.common.session import NO_TARGET
from tightrein.store.files import yaml_text
from tightrein.store.files.layout import UserLayout

ARCHIVE = """# 2026-09-20 修复报告

## 任务外发现

- `src/Services/OrderService.src:12` 查询没有按公司过滤，其他公司的订单也能查到
"""
DEPLOYED = {"protocol": 1, "status": "ok", "output": {"deployments": [
    {"id": "11", "commit": "a" * 40, "status": "succeeded", "environment": None,
     "url": "https://ci.example.test/runs/11", "createdAt": "2026-10-05T02:00:00Z"}]}}
DEPLOY_SOURCE = {"deploy-source": {"use": "core/github-actions", "options": {"workflow": "deploy.yml"}}}


def deploy_runner(request):
    """扩展进程的替身：deploy-source 返回一次成功部署。"""
    return ProcessOutcome(0, json.dumps(DEPLOYED).encode("utf-8"), b"")


def call_json(world, *argv):
    out = io.StringIO()
    code = main([*argv, "--workspace", str(world.root), "--json", "--now", "2026-10-05T12:00:00+09:00"],
                world.externals(), stdin=io.StringIO(), stdout=out, stderr=io.StringIO())
    return code, json.loads(out.getvalue())


def test_a_workspace_with_only_the_project_section(tmp_path):
    world = make_cli_world(tmp_path)
    project = {"project": {"name": "minimal", "repo": str(tmp_path / "repo"), "mainBranch": "main"}}
    (world.root / "project.yaml").write_text(yaml_text.dump(project), encoding="utf-8")
    out = io.StringIO()
    code = main(["collect", "deployments", "--workspace", str(world.root)], world.externals(), stdin=io.StringIO(),
                stdout=out, stderr=io.StringIO())
    assert code == 0 and DEPLOY_UNCONFIGURED in out.getvalue()
    code, values = call_json(world, "collect", "--probe", "api-fuzz")
    assert code == 0 and values["result"]["status"] == "skipped"
    handoff = json.loads((world.root / values["result"]["handoff"]).read_text(encoding="utf-8"))
    assert handoff["outputs"]["skippedReason"] == NO_TARGET
    assert world.vcs.commands == [] and world.transport.requests == []


def test_collect_aggregate_and_problem_commands(tmp_path):
    world = make_cli_world(tmp_path, extensions=DEPLOY_SOURCE)
    world.extension_runner = deploy_runner
    world.vcs.add(("rev-parse",), "1" * 40 + "\n")
    code, values = call_json(world, "collect", "deployments")
    assert code == 0 and values["result"]["commit"] == "a" * 40
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "2026-09-20-report.md").write_text(ARCHIVE, encoding="utf-8")
    code, values = call_json(world, "collect", "--probe", "incidental", "--import-archive", str(archive))
    assert code == 0 and values["result"]["status"] == "ok"
    run_id = values["result"]["runId"]
    code, values = call_json(world, "aggregate", "--select", f"run:{run_id}", "--reproduce", "skip")
    assert code == 0 and values["result"]["status"] == "ok"
    code, values = call_json(world, "problem", "ignore", "P-0001", "--reason", "已知问题", "--occurrences", "3")
    assert code == 0 and values["result"]["status"] == "ignored"
    code, values = call_json(world, "problem", "reopen", "P-0001", "--reason", "需要处理")
    assert code == 0
    code, values = call_json(world, "problem", "ignore", "P-0099", "--reason", "不存在")
    assert code == exit_codes.USAGE


def test_triage_selects_by_status_and_dry_runs(tmp_path):
    world = make_cli_world(tmp_path)
    base = make_triage_world(tmp_path)
    store_problem(base, "P-0001")
    base.conn.close()
    code, values = call_json(world, "triage", "--select", "status:new", "--dry-run")
    assert code == 0 and values["result"][0]["problemId"] == "P-0001"
    code, values = call_json(world, "triage", "--select", "7")
    assert code == exit_codes.USAGE
    code, values = call_json(world, "triage", "queue")
    assert code == 0 and values["result"] == []


def test_issue_create_and_approve_asks_for_the_fix_branch(tmp_path):
    world = make_cli_world(tmp_path)
    config = UserLayout(world.home).config()
    config.parent.mkdir(parents=True)
    config.write_text("branchPrefix: cty\n", encoding="utf-8")
    base = make_triage_world(tmp_path)
    store_triaged(base, "P-0001")
    base.conn.close()
    world.vcs.add(("rev-parse",), "1" * 40 + "\n")
    code, values = call_json(world, "issue", "create", "--select", "P-0001")
    assert code == 0 and values["result"]["items"][0]["issueId"] == "0001"
    code, values = call_json(world, "issue", "list", "--status", "needs-decision")
    assert [item["issueId"] for item in values["result"]] == ["0001"]
    code, values = call_json(world, "approve", "1")
    assert code == exit_codes.GATE, values
    assert values["pendingOperations"][0]["kind"] == "create-fix-worktree"
    assert values["next"] == "tightrein fix start 1" and values["result"]["issueStatus"] == "todo"
    code, values = call_json(world, "issue", "show", "1")
    assert code == 0 and values["result"]["status"] == "todo"
    code, values = call_json(world, "issue", "edit", "1")
    assert code == exit_codes.USAGE


def test_bare_issue_needs_input(tmp_path):
    world = make_cli_world(tmp_path)
    code, values = call_json(world, "issue")
    assert code == exit_codes.USAGE and "--input" in values["errors"][0]["message"]



def test_manual_issue_is_created_as_todo_and_approve_asks_for_the_fix_branch(tmp_path):
    world = make_cli_world(tmp_path)
    config = UserLayout(world.home).config()
    config.parent.mkdir(parents=True)
    config.write_text("branchPrefix: cty\n", encoding="utf-8")
    world.vcs.add(("rev-parse",), "1" * 40 + "\n")
    body = tmp_path / "need.md"
    body.write_text("导出按钮支持 CSV。\n\n## 验收标准\n\n- 点击导出得到 CSV 文件\n", encoding="utf-8")
    code, values = call_json(world, "new", "Export as CSV", "-f", str(body))
    assert code == 0, values
    result = values["result"]
    assert (result["issueId"], result["status"], result["origin"], result["problems"]) == (
        "0001", "todo", "manual", [])
    assert result["path"] == "issues/0001-export-as-csv.md"
    code, values = call_json(world, "approve", "1")
    assert code == exit_codes.GATE, values
    assert values["pendingOperations"][0]["kind"] == "create-fix-worktree"
    assert values["result"]["issueStatus"] == "todo"


def test_new_options_are_checked(tmp_path):
    world = make_cli_world(tmp_path)
    code, values = call_json(world, "new", "t", "-m", "x", "-f", "f")
    assert code == exit_codes.USAGE and "-m 与 -f" in values["errors"][0]["message"]
    code, values = call_json(world, "new", "t", "--select", "P-0001")
    assert code == exit_codes.USAGE and "--select" in values["errors"][0]["message"]
    code, values = call_json(world, "new", "只有标题")
    assert code == 0 and values["result"]["title"] == "只有标题"
    code, values = call_json(world, "issue", "create", "--manual", "--title", "t")
    assert code == exit_codes.USAGE and "已改为 `new`" in values["errors"][0]["message"]


def test_issue_sync_lists_the_github_mirror_result():
    assert _mirror_lines(None) == [] and _mirror_lines(MirrorReport()) == []
    assert _mirror_lines(MirrorReport(skipped="owner/name 是公开仓库", unsynced=["0001：未建 GitHub Issue"])) == [
        "GitHub 镜像未同步：owner/name 是公开仓库", "- 0001：未建 GitHub Issue"]
    lines = _mirror_lines(MirrorReport("owner/name", written=["0001"], operations=["OP-0003"], closed=["0002"]))
    assert lines == ["GitHub 镜像 owner/name：写入 1 个 Issue", "- 在 GitHub 上关闭，本地已按用户关闭处理：0002",
                     "- 待确认：tightrein approve OP-0003"]
