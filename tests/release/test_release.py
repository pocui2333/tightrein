"""发布的流程(本地真 git + 假 GitHub)：提交 → 推送 → PR → 等 CI → 合并一次走完并转为验收中；PR 被关闭且未合并时
以修复未采纳取消并记下用户说明；合并 main 改到同一文件时退回实施重新审查；冲突时转为待决定并留下冲突文件清单；
验收通过转为完成、写交付文档并清理本地；回归时先提撤销 PR(只提一次)、清理修复目录，再退回待修并开下一次修复尝试；不在发布中的不碰；前置条件过期时
留在发布中、下次重新观察；接入清单不启用验收时部署即完成、不观察。"""

import json
from datetime import timedelta

import pytest

from tightrein.onboard.setup import ModuleStatus
from tightrein.protocol.git import Stale
from tightrein.protocol.handoff import Status
from tightrein.protocol.process import Outcome
from tightrein.release import deploy
from tightrein.release import release as flow
from tightrein.release.accept.revert import Reverted
from tightrein.release.record import load_state
from tightrein.release.release import release
from tightrein.store.tables import issues, occurrences, problems


def _ready(kit, runtime, repos, worktree, **extra):
    repos.write(worktree, {"src/order.py": "PAGE = 0\n"})
    kit.deliver(runtime, "0007", kit.delivery_facts(worktree, ["src/order.py"]))
    return kit.new_issue(runtime, **extra)


def _accepting(kit, runtime, worktree, problem_ids=(), merged_at=None):
    kit.deliver(runtime, "0007", kit.delivery_facts(worktree, ["src/order.py"]))
    issue = kit.new_issue(runtime, status="accepting",
                          extra={"problems": list(problem_ids),
                                 "release": {"merged_at": merged_at or "2026-10-08T03:00:00Z"}})
    issue.merge_commit, issue.pr, issue.step = "m" * 40, 186, "release.accept"
    issues.save(runtime.conn, issue, runtime.clock)
    return issue


def _problem(runtime, kit, problem_id, source):
    problems.save(runtime.conn, problems.Problem(problem_id, f"fp-{problem_id}", source, "error", "new", "t",
                                                 kit.NOW - timedelta(days=1), kit.NOW - timedelta(days=1)),
                  runtime.clock)


def test_one_call_goes_from_commit_to_merge(kit, github_runtime, fake_github, repos, worktree, documents_written,
                                            simple_transitions):
    _ready(kit, github_runtime, repos, worktree)
    outcome = release(github_runtime, "0007")
    assert (outcome.status, outcome.next_point) == (Status.PASSED, "release.deploy")
    issue = issues.get(github_runtime.conn, "0007")
    assert issue.status == "accepting" and issue.pr == 186 and issue.merge_commit == "m" * 40
    pushed = repos.git(repos.origin, "rev-parse", f"refs/heads/{kit.BRANCH}").strip()
    assert load_state(issue).pushed == pushed
    assert ("pr-merge", 186, pushed, "squash", False) in fake_github.calls
    assert len(fake_github.pulls[186]["comments"]) == 1
    names = sorted(path.name for path in github_runtime.workspace.issue_dir("0007").glob("4*-handoff.json"))
    assert names == ["41-release.pr-handoff.json", "42-release.ci-handoff.json", "43-release.merge-handoff.json"]
    assert simple_transitions == [("0007", "merge", None)]


def test_waiting_for_ci_keeps_the_issue_releasing(kit, github_runtime, fake_github, repos, worktree):
    fake_github.runner.replies[("gh", "pr", "checks", "186", "--json")] = [
        Outcome(8, json.dumps([{"name": "test", "state": "PENDING", "bucket": "pending"}]), "", 1, None, None)]
    _ready(kit, github_runtime, repos, worktree)
    outcome = release(github_runtime, "0007")
    assert outcome.next_point == "release.merge" and "CI 检查还在进行" in outcome.summary
    assert issues.get(github_runtime.conn, "0007").status == "releasing"
    again = release(github_runtime, "0007")  # 第二次：什么都不重复做，照样等
    assert again.next_point == "release.merge"
    assert [call[0] for call in fake_github.calls].count("pr-create") == 1


def test_a_closed_pr_cancels_the_fix_and_keeps_the_user_note(kit, github_runtime, fake_github, repos, worktree,
                                                              simple_transitions):
    _ready(kit, github_runtime, repos, worktree)
    fake_github.create_pr(kit.BRANCH, "main", "t", "b", scope=None)
    fake_github.pulls[186]["state"] = "CLOSED"
    issue = issues.get(github_runtime.conn, "0007")
    issue.pr = 186
    issues.save(github_runtime.conn, issue, github_runtime.clock)
    fake_github.runner.replies[("gh", "pr", "view")] = [
        Outcome(0, json.dumps({"comments": [{"body": "先不改"}], "reviews": [{"body": "换个做法"}]}), "", 1, None, None)]
    outcome = release(github_runtime, "0007")
    assert "修复未采纳" in outcome.summary
    assert issues.get(github_runtime.conn, "0007").status == "cancelled"
    assert simple_transitions == [("0007", "fail", "fix_rejected")]


def test_main_touching_the_same_file_goes_back_to_review(kit, github_runtime, repos, worktree, simple_transitions):
    _ready(kit, github_runtime, repos, worktree)
    repos.commit(worktree, "fix: page", {"src/order.py": kit.FIX_ORDER})
    repos.upstream("feat: size", {"src/order.py": kit.MAIN_ORDER})
    outcome = release(github_runtime, "0007")
    assert outcome.next_point == "implement.review" and "src/order.py" in outcome.summary
    issue = issues.get(github_runtime.conn, "0007")
    assert issue.status == "implementing" and issue.step == "implement.review"
    assert repos.git(repos.origin, "branch", "--list", kit.BRANCH) == ""  # 没有推送


def test_conflicts_stop_for_a_decision(kit, github_runtime, repos, worktree, documents_written):
    _ready(kit, github_runtime, repos, worktree)
    repos.upstream("fix: page two", {"src/order.py": "PAGE = 2\n"})
    outcome = release(github_runtime, "0007")
    assert outcome.status == Status.PENDING and "冲突" in outcome.summary
    issue = issues.get(github_runtime.conn, "0007")
    assert issue.status == "needs_decision"
    assert load_state(issue).conflicts == ["src/order.py"]
    assert [kind for kind, _, _ in documents_written] == ["pending"]


def test_acceptance_waits_then_passes_and_cleans_up(kit, github_runtime, repos, worktree, documents_written,
                                                    simple_transitions):
    repos.commit(worktree, "fix: page", {"src/order.py": "PAGE = 0\n"})
    repos.git(worktree, "push", "-q", "-u", "origin", kit.BRANCH)
    _problem(github_runtime, kit, "P-0001", "collect.static")
    _accepting(kit, github_runtime, worktree, ["P-0001"])
    waiting = release(github_runtime, "0007")
    assert waiting.next_point == "release.deploy"  # 没有部署来源：合并后 1h 才视为已部署
    github_runtime.clock.advance(timedelta(hours=1))
    done = release(github_runtime, "0007")
    assert done.status == Status.PASSED and "验收通过" in done.summary
    issue = issues.get(github_runtime.conn, "0007")
    assert issue.status == "done" and issue.deploy == "m" * 40
    assert issue.extra["acceptUntil"] == "2026-10-08T04:00:00Z"  # 静态巡检：部署即到期
    assert ("deliver", "0007") in [(kind, subject) for kind, subject, _ in documents_written]
    assert not worktree.exists() and load_state(issue).cleanup == "done"
    assert simple_transitions == [("0007", "accept", None)]


def test_without_acceptance_a_deployed_merge_is_done_without_watching(kit, tmp_path, repos, fake_github, worktree,
                                                                     documents_written, simple_transitions):
    runtime = kit.make_runtime(tmp_path, repos.repo, github=fake_github,
                               setup=kit.make_setup(accept=ModuleStatus.DISABLED))
    _problem(runtime, kit, "P-0001", "collect.platform_errors")  # 运行时来源本要观察 24h
    _accepting(kit, runtime, worktree, ["P-0001"])
    runtime.clock.advance(timedelta(hours=1))  # 没有部署来源：合并后 1h 视为已部署
    done = release(runtime, "0007")
    assert done.status == Status.PASSED and "不观察回归" in done.summary
    issue = issues.get(runtime.conn, "0007")
    assert issue.status == "done" and load_state(issue).accept == {"result": "skipped"}
    assert "acceptUntil" not in issue.extra
    assert simple_transitions == [("0007", "accept", None)]
    runtime.conn.close()


def test_a_regression_opens_one_revert_then_goes_back_to_todo(kit, github_runtime, repos, worktree,
                                                               documents_written, monkeypatch, simple_transitions):
    _problem(github_runtime, kit, "P-0001", "collect.platform_errors")
    _accepting(kit, github_runtime, worktree, ["P-0001"], merged_at="2026-10-08T01:00:00Z")
    occurrences.add(github_runtime.conn, occurrences.Occurrence("P-0001", kit.NOW + timedelta(minutes=5),
                                                                "collect.platform_errors"), github_runtime.clock)
    github_runtime.clock.advance(timedelta(minutes=10))
    reverts = []

    def revert(runtime, issue, merge_commit, reason, conventions, github):
        reverts.append(merge_commit)
        return Reverted("hotfix/7-revert", 190, "https://github.com/cty/sample/pull/190")

    monkeypatch.setattr(flow.revert, "revert", revert)
    moving = flow.move

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(flow, "move", interrupted)  # 提了撤销、退回待修之前中断：再来一次不重复提撤销
    with pytest.raises(KeyboardInterrupt):
        release(github_runtime, "0007")
    assert issues.get(github_runtime.conn, "0007").extra["acceptUntil"] == "2026-10-09T02:00:00Z"  # 部署后 1h 加 24h
    monkeypatch.setattr(flow, "move", moving)
    outcome = release(github_runtime, "0007")
    assert outcome.status == Status.FAILED and "pull/190" in outcome.summary
    assert reverts == ["m" * 40]
    assert simple_transitions == [("0007", "regress", None)]
    # 退回待修即开下一次修复尝试：这一次的 PR、合并提交与发布进度(含撤销 PR)留档后清空，下一次重新提交、提 PR
    issue = issues.get(github_runtime.conn, "0007")
    assert issue.status == "todo" and issue.extra["attempt"] == 2
    assert (issue.pr, issue.merge_commit, load_state(issue).revert, load_state(issue).merged_at) == (None, None, None,
                                                                                                    None)
    (first,) = issue.extra["attempts"]
    assert first["release"]["revert"]["pr"] == 190 and first["mergeCommit"] == "m" * 40
    assert first["release"]["cleanup"] is not None  # 这一次的修复目录已清理(或写明为什么保留)


def test_a_failing_read_only_query_skips_only_that_issue_with_the_reason(kit, github_runtime, worktree,
                                                                         monkeypatch):
    _accepting(kit, github_runtime, worktree)

    def broken(runtime, merge_commit, merged_at):
        raise deploy.DeployError(deploy.DeployErrorKind.UNAVAILABLE, "gh 超时")

    monkeypatch.setattr(flow.deploy, "track", broken)
    outcome = release(github_runtime, "0007")
    assert (outcome.status, outcome.next_point) == (Status.PASSED, "release.deploy")
    assert "读取部署记录失败，下次再试：unavailable：gh 超时" in outcome.summary
    assert issues.get(github_runtime.conn, "0007").status == "accepting"  # 不改状态，下次再查
    other = kit.new_issue(github_runtime, status="todo", issue_id="0008")
    assert release(github_runtime, other.id).summary == "Issue 当前为 todo，不在发布中"  # 其他 Issue 照常处理


def test_a_failed_deployment_is_only_reported_with_its_link_for_a_person_to_judge(kit, github_runtime, worktree,
                                                                                   documents_written, monkeypatch):
    _accepting(kit, github_runtime, worktree)
    record = deploy.DeployRecord("d1", "m" * 40, deploy.FAILED, "production", "https://ci.example.test/runs/9",
                                 kit.NOW)
    monkeypatch.setattr(flow.deploy, "track",
                        lambda runtime, merge_commit, merged_at: deploy.Deployed(deploy.FAILED, record,
                                                                                 "github_actions", None))
    outcome = release(github_runtime, "0007")
    assert outcome.status == Status.PENDING
    assert "https://ci.example.test/runs/9" in outcome.summary and "是否由本修复引起请人判断" in outcome.summary
    issue = issues.get(github_runtime.conn, "0007")
    assert issue.status == "accepting" and load_state(issue).deploy["status"] == "failed"  # 不回滚、不判回归
    assert [kind for kind, _, _ in documents_written] == ["pending"]
    release(github_runtime, "0007")
    assert [kind for kind, _, _ in documents_written] == ["pending"]  # 同一次失败只交人一次


def test_issues_outside_release_are_left_alone(kit, runtime):
    kit.new_issue(runtime, status="todo")
    assert release(runtime, "0007").summary == "Issue 当前为 todo，不在发布中"


def test_a_stale_decision_waits_for_the_next_run(kit, github_runtime, fake_github, repos, worktree, documents_written,
                                                 simple_transitions, monkeypatch):
    _ready(kit, github_runtime, repos, worktree)

    def stale(*args, **kwargs):
        raise Stale("push", ["head"])

    monkeypatch.setattr(flow.push, "push", stale)
    outcome = release(github_runtime, "0007")
    assert (outcome.status, outcome.next_point) == (Status.PASSED, "release.pr")
    assert "前置条件已变化" in outcome.summary and "下次运行重新观察" in outcome.summary
    assert issues.get(github_runtime.conn, "0007").status == "releasing"  # 不转为待决定
    assert documents_written == [] and simple_transitions == []
