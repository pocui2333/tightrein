from dataclasses import replace

import pytest
from fix_world import SERVICE_PATH
from pipeline_world import PROJECT
from release_world import BRANCH, Planner, to_submit

from tightrein.domain.enums import IssuePhase, HandoffStatus, IssueStatus, OperationKind, RunStage
from tightrein.domain.issue import GithubLink
from tightrein.pipeline.issue.steps import transitions
from tightrein.pipeline.issue.steps.transitions import IssueEnv
from tightrein.pipeline.common import stage_runs
from tightrein.pipeline.release.service import ReleaseDeps, ReleaseService
from tightrein.pipeline.common import conventions
from tightrein.pipeline.release.steps import pull_request, sync
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import issues, pulls
from tightrein.vcs.parse import Commit

FAILURE = {"check": "residue", "location": "src/a.src:3", "problem": "新增了调试输出", "category": "local"}


def releaser(world, git, planner=None, layout=None):
    return ReleaseService(ReleaseDeps(layout or world.layout, world.config, world.conn, world.clock, world.events, git,
                                      planner or Planner()))


def outputs(world):
    return stage_runs.latest_outputs(world.conn, world.layout, RunStage.RELEASE, world.issue_id)[1]


def history(world):
    return (world.layout.root / issues.get(world.conn, world.issue_id).path).read_text(encoding="utf-8")


def test_commit_needs_passing_checks_or_accepted_findings(tmp_path):
    world, git = to_submit(tmp_path, failures=[FAILURE])
    planner = Planner()
    blocked = releaser(world, git, planner).commit(world.issue_id)
    assert blocked.status is HandoffStatus.BLOCKED and "residue src/a.src:3 新增了调试输出" in blocked.message
    assert "--accept-findings" in blocked.message and planner.calls == []
    result = releaser(world, git, planner).commit(world.issue_id, accept_findings=True)
    assert result.operation == "OP-0001" and planner.calls == [(OperationKind.COMMIT, (SERVICE_PATH,))]
    assert "fix: 订单查询返回 500\n\n在 OrderService.Get 中按公司过滤" in result.message
    assert "接受的未通过项" in result.message
    assert outputs(world)["acceptedFindings"] == [{k: v for k, v in FAILURE.items() if k != "category"}]
    assert "照常提交，接受的未通过项：residue 新增了调试输出" in history(world)


def test_commit_stops_when_the_worktree_changed_after_apply(tmp_path):
    world, git = to_submit(tmp_path)
    (world.worktree / "notes.txt").write_text("x\n", encoding="utf-8")
    result = releaser(world, git).commit(world.issue_id)
    assert result.status is HandoffStatus.BLOCKED and "修复之后新增的改动：notes.txt" in result.message


def test_sync_merges_only_when_main_moved_and_reports_conflicts(tmp_path):
    world, git = to_submit(tmp_path)
    service = releaser(world, git)
    assert service.sync(world.issue_id).message == "origin/main 没有新提交"
    git.incoming = [Commit("d" * 40, "li", world.clock.now(), "feat: 调整订单")]
    git.base = {path: (world.worktree / path).read_text() for path in git.base}
    result = service.sync(world.issue_id)
    assert result.operation is not None and "dddddddddddd feat: 调整订单" in result.message
    assert f"与本修复改动文件的交集：{SERVICE_PATH}" in result.message
    git.merging = "d" * 40
    git.conflicts = (SERVICE_PATH,)
    (world.worktree / SERVICE_PATH).write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> origin/main\n",
                                               encoding="utf-8")
    reported = service.sync(world.issue_id)
    assert reported.status is HandoffStatus.BLOCKED and "互斥实现" in reported.path.read_text(encoding="utf-8")
    assert service.sync(world.issue_id, cont=True).message == f"还有冲突标记：{SERVICE_PATH}"
    git.conflicts = ()
    git.versions = {("HEAD", SERVICE_PATH): "ours\n", ("MERGE_HEAD", SERVICE_PATH): "theirs\n"}
    (world.worktree / SERVICE_PATH).write_text("ours\ntheirs\n", encoding="utf-8")
    resolved = service.sync(world.issue_id, cont=True)
    assert resolved.operation is not None and f"{SERVICE_PATH}：both" in resolved.message


def test_sync_overlap_counts_only_files_main_changed(tmp_path):
    world, git = to_submit(tmp_path)
    git.incoming = [Commit("d" * 40, "li", world.clock.now(), "docs: readme")]
    git.base = {path: (world.worktree / path).read_text() for path in git.base}
    git.main_paths = ("README.md",)
    assert "与本修复改动文件的交集：无" in releaser(world, git).sync(world.issue_id).message


def test_conflict_resolutions():
    assert sync.resolution("a\n", "b\n", "a\n") == "fix-side"
    assert sync.resolution("a\n", "b\n", "b\n") == "main-side"
    assert sync.resolution("a\n", "b\n", "a\nb\n") == "both"
    assert sync.hunks("x\n<<<<<<< HEAD\n1\n=======\n2\n3\n>>>>>>> m\n") == [("1", "2\n3")]


def test_push_waits_for_main_and_lists_the_commits(tmp_path):
    world, git = to_submit(tmp_path)
    service = releaser(world, git)
    git.incoming = [Commit("d" * 40, "li", world.clock.now(), "feat")]
    assert "先执行 release sync" in service.push(world.issue_id).message
    git.incoming = []
    result = service.push(world.issue_id)
    assert result.operation is not None and "2222222 fix: 修复订单查询返回 500" in result.message


def test_titles_branch_names_and_the_pr_body(tmp_path, make_config):
    config = make_config()
    generic = conventions.resolve(config, tmp_path)
    personal = replace(generic, personal_prefix=True)
    assert pull_request.branch_problem("bugfix/3-export-key", generic, config) is None
    assert "不符合" in pull_request.branch_problem("cty/fix-x", generic, config)
    assert pull_request.branch_problem("claude/bugfix/3-x", personal, config) == "分支前缀 claude 是 AI 或工具名称"
    world, git = to_submit(tmp_path)
    issue = world.issue()
    assert pull_request.suggest(issue, None, generic, config) == f"bugfix/{int(issue.id)}-{issue.slug}"
    assert pull_request.suggest(issue, "claude", personal, config).startswith("<个人前缀>/bugfix/")
    planner = Planner()
    result = releaser(world, git, planner).pr(world.issue_id)
    assert result.operation is not None and planner.title == issue.title
    body = planner.body
    assert not [line for line in body.splitlines() if line.startswith("## ")]
    assert "在 OrderService.Get 中按公司过滤" in body and "验证结果(程序生成)：" in body
    assert "- 其他 Issue 的复现检查：1 条，1 条通过" in body and "- 未验证：计算节点的流程(不在本机启动)" in body
    assert body.index("在 OrderService.Get") < body.index("验证结果")
    assert releaser(world, git, Planner(unchanged_pr=True)).pr(world.issue_id).message == "PR 描述没有变化，不需要更新"
    world2, git2 = to_submit(tmp_path / "bad", branch="claude/fix-order-500")
    assert "建议改名为 bugfix/" in releaser(world2, git2).pr(world2.issue_id).message


def test_the_executor_release_text_and_a_pr_template_shape_the_pr(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".github").mkdir(parents=True)
    (repo / ".github" / "pull_request_template.md").write_text(
        "## Summary\n\n<!-- what and why -->\n\n## Checklist\n\n- [ ] docs updated\n\n## How was this tested?\n\n",
        encoding="utf-8")
    world, git = to_submit(tmp_path / "w", project={**PROJECT["project"], "repo": str(repo)})
    fix = dict(stage_runs.latest_outputs(world.conn, world.layout, RunStage.FIX, world.issue_id)[1])
    fix["release"] = {"scope": "订单", "subject": "订单查询按公司过滤", "why": "跨公司读取。", "prTitle": "按公司过滤订单查询",
                      "problem": "其他公司的订单可以被查到。", "approach": "在入口过滤。", "limitations": "缓存未处理。"}
    run = stage_runs.begin(RunStage.FIX, world.layout, world.conn, world.clock, world.events)
    run.handoff(RunStage.FIX, world.issue_id, HandoffStatus.OK, fix, "fix done")
    planner = Planner()
    releaser(world, git, planner).pr(world.issue_id)
    assert planner.title == "按公司过滤订单查询"
    assert "## Summary\n\n其他公司的订单可以被查到。\n\n在入口过滤。\n\n缓存未处理。" in planner.body
    assert "## Checklist\n\n- [ ] docs updated" in planner.body
    assert "## How was this tested?\n\n- " in planner.body
    message = releaser(world, git, planner).commit(world.issue_id).message
    assert message.startswith("提交信息：\nfix(订单): 订单查询按公司过滤\n\n跨公司读取。") or \
        "fix(订单): 订单查询按公司过滤\n\n跨公司读取。" in message


@pytest.mark.parametrize("url, line", [("https://github.com/cty/sample/issues/12", "Closes #12"),
                                       ("https://github.com/cty/tracker/issues/12", "Closes cty/tracker#12")])
def test_the_pr_body_closes_the_github_mirror(tmp_path, url, line):
    world, git = to_submit(tmp_path)
    transitions.annotate(IssueEnv(world.conn, world.layout, world.clock, world.config), world.issue_id, "镜像",
                         updates={"github": GithubLink(12, url)})
    planner = Planner()
    releaser(world, git, planner).pr(world.issue_id)
    assert f"\n{line}\n" in planner.body and planner.body.index(line) < planner.body.index("验证结果")


def test_follow_ups_record_results_and_move_the_issue(tmp_path):
    world, git = to_submit(tmp_path)
    planner = Planner()
    service = releaser(world, git, planner)
    committed = service.commit(world.issue_id)
    service.on_executed(planner._operation(OperationKind.COMMIT, world.issue_id, (SERVICE_PATH,),
                                           "提交信息：\nfix: 修复订单查询返回 500", {"branch": BRANCH, "commit": "2" * 40}))
    assert outputs(world)["commits"][0]["message"] == "fix: 修复订单查询返回 500"
    assert committed.operation == "OP-0001"
    service.on_executed(planner.plan_push(world.issue_id, None, BRANCH))
    assert outputs(world)["push"]["remoteBranch"] == f"origin/{BRANCH}"
    service.pr(world.issue_id)
    service.on_executed(planner.plan_pull_request(world.issue_id, None, BRANCH, "t", "b"))
    issue = world.issue()
    assert (issue.status, issue.pr) == (IssueStatus.PENDING_MERGE, "https://example.test/pull/187")
    assert pulls.get(world.conn, world.issue_id).number == 187 and outputs(world)["pr"]["state"] == "open"
    world2, git2 = to_submit(tmp_path / "merge")
    service2 = releaser(world2, git2, planner)
    service2.on_executed(planner.plan_merge_main(world2.issue_id, None))
    assert world2.issue().phase is IssuePhase.VERIFY and outputs(world2)["syncs"][0]["mainCommit"] == "d" * 40


def test_output_mode_only_renders(tmp_path):
    world, git = to_submit(tmp_path)
    planner = Planner()
    layout = WorkspaceLayout(world.layout.root, tmp_path / "out")
    result = releaser(world, git, planner, layout).commit(world.issue_id)
    assert result.status is HandoffStatus.OK and planner.calls == []
    assert (tmp_path / "out" / "commit.md").read_text(encoding="utf-8").startswith("提交信息：")
    releaser(world, git, planner, layout).pr(world.issue_id)
    assert (tmp_path / "out" / "pr-title.txt").read_text(encoding="utf-8") == "订单查询返回 500\n"
    assert planner.calls == []
