"""发布的流程(本地真 git + 假 GitHub)：提交 → 推送 → PR → 等 CI → 合并一次走完并转为验收中；PR 被关闭且未合并时
以修复未采纳取消并记下用户说明；合并 main 改到同一文件时退回实施重新自检与审查；冲突时退回编码解决，退回次数用完后转为待决定并留下冲突文件清单；
合并停在进行中时先处理再提交(没解决且退回次数没用完时放弃、退回编码，用户解决了就完成合并、退回自检与审查，次数用完继续等人)；
同步干净、推送成功后退回次数清零；推送前要推送的改动与交付时审查过的不同(人自己提交了合并、交付之后另有提交)时退回自检与
审查，合并的 main 只改了别的文件时照常推送；
高风险路径停在合并关卡，用户通过后由发布合并(等 CI 期间通过的也不丢)，交付带用户接受的未通过项时文档请人在 GitHub 上合并；
等 CI 期间被接管的不再往下做；
验收通过转为完成、写交付文档并清理本地；回归时先提撤销 PR(只提一次)、清理修复目录，再退回待修并开下一次修复尝试；推送撤销分支一直
遇到服务器错误时留在验收中、这一步记为失败、下次再提；不在发布中的不碰；前置条件过期时留在发布中、下次重新观察；推送一直遇到服务器错误时
留在发布中、下次再推送；接入清单不启用验收时部署即完成、不观察。"""

import json
from datetime import timedelta

import pytest

from tightrein.assess.issue import transitions
from tightrein.onboard.setup import ModuleStatus
from tightrein.protocol.git import Stale, TransientError
from tightrein.protocol.handoff import Status
from tightrein.protocol.process import Outcome
from tightrein.release import deploy
from tightrein.release import release as flow
from tightrein.release.accept.revert import Reverted
from tightrein.release.record import load_state
from tightrein.release.release import release
from tightrein.store.tables import issues, occurrences, problems

PUSH = ("git", "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60", "push")


def _ready(kit, runtime, repos, worktree, **extra):
    repos.write(worktree, {"src/order.py": "PAGE = 0\n"})
    delivered = kit.diff_hash(runtime, worktree)  # 推送前据此核对要推送的就是审查过的改动
    kit.deliver(runtime, "0007", kit.delivery_facts(worktree, ["src/order.py"], diffHash=delivered))
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
    assert fake_github.pulls[186]["comments"] == []  # 缺省不在 PR 下贴审查结论(开发者不会给自己的 PR 贴审查报告)
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
    assert outcome.next_point == "implement.check" and "src/order.py" in outcome.summary
    issue = issues.get(github_runtime.conn, "0007")
    assert issue.status == "implementing" and issue.step == "implement.check"
    assert repos.git(repos.origin, "branch", "--list", kit.BRANCH) == ""  # 没有推送
    facts = _handoff_facts(github_runtime)
    # 实施据 backTo 接着做；合并过 main 后 HEAD 变了，检查点取合并之后的 commit
    assert facts["backTo"] == "implement.check" and facts["worktreeCommit"] == repos.head(worktree)


def test_conflicts_go_back_to_coding_until_the_returns_are_used_up(kit, github_runtime, repos, worktree,
                                                                   documents_written, simple_transitions):
    _ready(kit, github_runtime, repos, worktree)
    main = repos.upstream("fix: page two", {"src/order.py": "PAGE = 2\n"})
    outcome = release(github_runtime, "0007")
    assert (outcome.status, outcome.next_point) == (Status.PASSED, "implement.code") and "冲突" in outcome.summary
    issue = issues.get(github_runtime.conn, "0007")
    assert issue.status == "implementing" and issue.step == "implement.code"
    assert load_state(issue).conflict_returns == 1 and documents_written == []
    facts = _handoff_facts(github_runtime)
    assert facts["backTo"] == "implement.code" and facts["main"] == main and facts["conflicts"] == ["src/order.py"]
    assert facts["mainCommits"] == [f"{main[:12]} fix: page two"] and facts["returns"] == 1
    assert facts["worktreeCommit"] == repos.head(worktree)
    assert _clean(github_runtime, worktree)  # 发布放弃了这次合并
    assert simple_transitions == [("0007", "start", None)]

    # 退回次数用完(release.conflictReturns 缺省 2)：照旧停下等人
    issue.status, issue.extra = "releasing", {**issue.extra, "release": {**issue.extra["release"],
                                                                         "conflict_returns": 2}}
    issues.save(github_runtime.conn, issue, github_runtime.clock)
    blocked = release(github_runtime, "0007")
    assert blocked.status == Status.PENDING and "已退回编码解决 2 次仍冲突" in blocked.summary
    issue = issues.get(github_runtime.conn, "0007")
    assert issue.status == "needs_decision"
    assert load_state(issue).conflicts == ["src/order.py"]
    assert [kind for kind, _, _ in documents_written] == ["pending"]


def _stuck(kit, runtime, repos, worktree, **release_state):
    """交给人的冲突留下的现场(旧版发布冲突时，或退回编码次数用完后)：修复已提交，合并 origin/main 停在进行中，
    冲突文件里有冲突标记，发布进度记着冲突文件清单。"""
    fix = repos.commit(worktree, "fix: page", {"src/order.py": "PAGE = 0\n"})
    delivered = kit.diff_hash(runtime, worktree)
    kit.deliver(runtime, "0007", kit.delivery_facts(worktree, ["src/order.py"], diffHash=delivered))
    main = repos.upstream("fix: page two", {"src/order.py": "PAGE = 2\n"})
    repos.merge_main(worktree)
    kit.new_issue(runtime, extra={"release": {"conflicts": ["src/order.py"], **release_state}})
    return fix, main


def test_a_merge_left_by_the_old_release_goes_back_to_coding_after_approval(kit, github_runtime, repos, worktree,
                                                                            documents_written, simple_transitions):
    fix, main = _stuck(kit, github_runtime, repos, worktree)  # 旧版的发布进度里没有退回次数
    issue = issues.get(github_runtime.conn, "0007")
    issue.status, issue.stage = "needs_decision", "release"
    issue.extra = {**issue.extra, "hold": {"reason": "合并 origin/main 出现冲突：src/order.py",
                                           "since": "2026-10-08T02:00:00Z", "stage": "release", "status": "releasing"}}
    issues.save(github_runtime.conn, issue, github_runtime.clock)
    assert transitions.approve(github_runtime, "0007").status == "releasing"  # tightrein approve：回到发布中
    outcome = release(github_runtime, "0007")
    # 放弃那次合并、重新同步时再冲突，按新的做法退回编码；没有把半截合并当成修复提交，也没有推送
    assert (outcome.status, outcome.next_point) == (Status.PASSED, "implement.code")
    issue = issues.get(github_runtime.conn, "0007")
    assert issue.status == "implementing" and issue.step == "implement.code"
    assert load_state(issue).conflict_returns == 1 and load_state(issue).conflicts == []
    facts = _handoff_facts(github_runtime)
    assert facts["backTo"] == "implement.code" and facts["main"] == main and facts["conflicts"] == ["src/order.py"]
    assert repos.head(worktree) == fix and facts["worktreeCommit"] == fix and _clean(github_runtime, worktree)
    assert repos.git(repos.origin, "branch", "--list", kit.BRANCH) == ""
    assert documents_written == [] and simple_transitions == [("0007", "approve", None), ("0007", "start", None)]


@pytest.mark.parametrize("returns", [0, 2])
def test_a_merge_the_user_resolved_is_finished_before_committing(kit, github_runtime, repos, worktree,
                                                                 simple_transitions, returns):
    fix, main = _stuck(kit, github_runtime, repos, worktree, conflict_returns=returns)
    repos.write(worktree, {"src/order.py": "PAGE = 2\n"})
    repos.git(worktree, "add", "src/order.py")
    outcome = release(github_runtime, "0007")
    assert outcome.next_point == "implement.check" and "解决过冲突：src/order.py(main-side)" in outcome.summary
    issue = issues.get(github_runtime.conn, "0007")
    assert issue.status == "implementing" and issue.step == "implement.check"
    state = load_state(issue)
    assert state.conflicts == [] and state.synced_main == main and state.conflict_returns == returns
    # 合并提交由程序完成，用合并的提交信息，不是修复的；之后退回重新自检与审查，不推送
    assert repos.git(worktree, "log", "-1", "--format=%P%n%s").split("\n")[:2] == [
        f"{fix} {main}", f"Merge origin/main into {kit.BRANCH}"]
    facts = _handoff_facts(github_runtime)
    assert facts["resolutions"] == {"src/order.py": "main-side"} and facts["worktreeCommit"] == repos.head(worktree)
    assert repos.git(repos.origin, "branch", "--list", kit.BRANCH) == ""
    assert simple_transitions == [("0007", "start", None)]


def test_an_unresolved_merge_keeps_waiting_once_the_returns_are_used_up(kit, github_runtime, repos, worktree,
                                                                        documents_written):
    fix, _ = _stuck(kit, github_runtime, repos, worktree, conflict_returns=2)
    repos.git(worktree, "add", "src/order.py")  # git add 了，冲突标记还在
    outcome = release(github_runtime, "0007")
    assert outcome.status == Status.PENDING and "还有没解决的冲突：src/order.py" in outcome.summary
    issue = issues.get(github_runtime.conn, "0007")
    assert issue.status == "needs_decision" and load_state(issue).conflicts == ["src/order.py"]
    # 合并原样停在进行中，没有提交、没有推送
    assert github_runtime.git.at(worktree).merge_head() is not None and repos.head(worktree) == fix
    assert (worktree / "src/order.py").read_text(encoding="utf-8").startswith("<<<<<<< ")
    assert repos.git(repos.origin, "branch", "--list", kit.BRANCH) == ""
    assert [kind for kind, _, _ in documents_written] == ["pending"]
    assert documents_written[0][2]["options"] == ["在修复目录解决冲突并 git add 后重新发布",
                                                  "放弃这次合并(git merge --abort)后重新发布"]


def test_a_clean_sync_and_push_resets_the_conflict_returns(kit, github_runtime, repos, worktree):
    _ready(kit, github_runtime, repos, worktree, extra={"release": {"conflict_returns": 1}})
    release(github_runtime, "0007")
    assert load_state(issues.get(github_runtime.conn, "0007")).conflict_returns == 0


@pytest.mark.parametrize("returns", [0, 2])
def test_a_merge_the_user_committed_goes_back_to_self_check_unpushed(kit, github_runtime, repos, worktree,
                                                                     simple_transitions, returns):
    _stuck(kit, github_runtime, repos, worktree, conflict_returns=returns)
    repos.write(worktree, {"src/order.py": "PAGE = 2\n"})
    repos.git(worktree, "add", "src/order.py")
    repos.git(worktree, "commit", "-q", "--no-edit")  # 解决后自己提交了合并：合并已不在进行中，main 已是祖先
    merged = repos.head(worktree)
    outcome = release(github_runtime, "0007")
    assert outcome.next_point == "implement.check" and "与审查过的不同" in outcome.summary
    issue = issues.get(github_runtime.conn, "0007")
    assert issue.status == "implementing" and issue.step == "implement.check"
    assert load_state(issue).conflicts == []  # 合并已不在进行中，冲突文件清单作废
    facts = _handoff_facts(github_runtime)
    assert facts["backTo"] == "implement.check" and facts["worktreeCommit"] == merged == repos.head(worktree)
    assert facts["diffHash"] != facts["deliveredDiffHash"]
    assert repos.git(repos.origin, "branch", "--list", kit.BRANCH) == ""  # 没经自检与审查，不推送
    assert simple_transitions == [("0007", "start", None)]


def test_a_commit_made_after_delivery_goes_back_to_self_check(kit, github_runtime, repos, worktree):
    _ready(kit, github_runtime, repos, worktree)
    repos.commit(worktree, "fix: page", {"src/order.py": "PAGE = 3\n"})  # 交付之后有人在修复目录改了又提交
    outcome = release(github_runtime, "0007")
    assert outcome.next_point == "implement.check"
    assert issues.get(github_runtime.conn, "0007").step == "implement.check"
    assert repos.git(repos.origin, "branch", "--list", kit.BRANCH) == ""


def test_main_changing_other_files_is_merged_and_pushed_without_review(kit, github_runtime, repos, worktree):
    _ready(kit, github_runtime, repos, worktree)
    main = repos.upstream("feat: user", {"src/user.py": "NAME = 'b'\n"})
    outcome = release(github_runtime, "0007")
    # 合并的 main 只改了别的文件：相对它的 diff 哈希与交付时相同，照常推送、合并
    assert outcome.next_point == "release.deploy"
    issue = issues.get(github_runtime.conn, "0007")
    assert issue.status == "accepting" and load_state(issue).synced_main == main
def test_approving_the_high_risk_gate_lets_release_merge_the_pr(kit, github_runtime, fake_github, repos, worktree,
                                                                documents_written, simple_transitions):
    from dataclasses import replace

    from tightrein.implement.approve.approve import record
    from tightrein.implement.context import Decision

    _gated(kit, github_runtime, repos, worktree)
    assert issues.get(github_runtime.conn, "0007").status == "releasing"
    [(kind, _, written)] = documents_written
    assert kind == "pending" and written["command"] == "tightrein approve 0007"
    assert written["options"][:2] == ["审阅改动后由 tightrein 合并：tightrein approve 0007",
                                      "自己在 GitHub 上合并：tightrein approve 0007 --option 2，或直接合并"]
    assert not any(call[0] == "pr-merge" for call in fake_github.calls)

    # tightrein approve 0007：记下决定、清掉 gate(cli/commands/approve.py)
    record(github_runtime, "0007", Decision("high_risk_merge", "approve", None, "只加了一个依赖", "2026-10-08T03:00:00Z"))
    issues.save(github_runtime.conn, replace(issues.get(github_runtime.conn, "0007"), gate=None), github_runtime.clock)
    outcome = release(github_runtime, "0007")
    assert (outcome.status, outcome.next_point) == (Status.PASSED, "release.deploy")
    assert issues.get(github_runtime.conn, "0007").status == "accepting"
    pushed = repos.git(repos.origin, "rev-parse", f"refs/heads/{kit.BRANCH}").strip()
    assert ("pr-merge", 186, pushed, "squash", False) in fake_github.calls
    path = github_runtime.workspace.issue_dir("0007") / "43-release.merge-handoff.json"
    facts = json.loads(path.read_text(encoding="utf-8"))["facts"]
    assert facts["merged"] and [item["point"] for item in facts["approvals"]] == ["high_risk_merge"]
    assert simple_transitions == [("0007", "merge", None)]


def _gated(kit, runtime, repos, worktree, **facts):
    """交付改了 requirements.txt(高风险路径)，第一次发布停在 high_risk_merge。"""
    repos.write(worktree, {"requirements.txt": "fastembed>=0.4.0\n"})
    kit.deliver(runtime, "0007", kit.delivery_facts(worktree, ["requirements.txt"], highRiskPaths=["requirements.txt"],
                                                    **facts))
    kit.new_issue(runtime)
    gated = release(runtime, "0007")
    assert gated.status == Status.PENDING and issues.get(runtime.conn, "0007").gate == "high_risk_merge"


def _meanwhile(monkeypatch, change):
    """等 CI 的时候(ci.wait 阻塞期间)别处改了 Issue 记录：change(runtime) 模拟命令行的操作。"""
    real = flow.ci.wait

    def wait(runtime, github, number, *, required):
        change(runtime)
        return real(runtime, github, number, required=required)

    monkeypatch.setattr(flow.ci, "wait", wait)


def test_an_approval_given_while_waiting_for_ci_is_not_lost(kit, github_runtime, fake_github, repos, worktree,
                                                             documents_written, simple_transitions, monkeypatch):
    from dataclasses import replace

    from tightrein.implement.approve.approve import record
    from tightrein.implement.context import Decision

    _gated(kit, github_runtime, repos, worktree)

    def approve(runtime):  # tightrein approve 0007：记下决定、清掉 gate(cli/commands/approve.py)
        record(runtime, "0007", Decision("high_risk_merge", "approve", None, "看过了", "2026-10-08T03:00:00Z"))
        issues.save(runtime.conn, replace(issues.get(runtime.conn, "0007"), gate=None), runtime.clock)

    _meanwhile(monkeypatch, approve)
    outcome = release(github_runtime, "0007")
    assert (outcome.status, outcome.next_point) == (Status.PASSED, "release.deploy")
    issue = issues.get(github_runtime.conn, "0007")
    assert issue.status == "accepting" and issue.gate is None
    assert [item["verdict"] for item in issue.extra["decisions"]] == ["approve"]  # 用户的决定还在记录里
    assert len(documents_written) == 1 and simple_transitions == [("0007", "merge", None)]


def test_an_issue_taken_over_while_waiting_for_ci_is_left_alone(kit, github_runtime, fake_github, repos, worktree,
                                                                 simple_transitions, monkeypatch):
    from dataclasses import replace

    _ready(kit, github_runtime, repos, worktree)
    _meanwhile(monkeypatch, lambda runtime: issues.save(
        runtime.conn, replace(issues.get(runtime.conn, "0007"), status="held", held_by="cty"), runtime.clock))
    outcome = release(github_runtime, "0007")
    assert (outcome.status, outcome.next_point) == (Status.PASSED, None) and "由 cty 接管" in outcome.summary
    issue = issues.get(github_runtime.conn, "0007")
    assert (issue.status, issue.held_by) == ("held", "cty")
    assert not any(call[0] == "pr-merge" for call in fake_github.calls) and simple_transitions == []


def test_a_gate_on_accepted_findings_asks_for_a_merge_on_github(kit, github_runtime, fake_github, repos, worktree,
                                                                documents_written):
    _gated(kit, github_runtime, repos, worktree, acceptedFindings=["ruff：行太长"])
    [(kind, _, written)] = documents_written
    assert kind == "pending" and written["command"] == "tightrein run --object 0007"
    assert written["options"][0] == "审阅改动后在 GitHub 上合并"
    assert not any("tightrein approve" in option for option in written["options"])
    assert "在 GitHub 上合并" in written["recommendation"] and "tightrein" not in written["recommendation"]
    assert written["reason"].startswith("改动涉及高风险路径，不自动合并：requirements.txt；最后一轮检查没有全部通过")
    assert "本工具不合并这次交付" in written["reason"]
    assert not any(call[0] == "pr-merge" for call in fake_github.calls)


def _handoff_facts(runtime):
    path = runtime.workspace.issue_dir("0007") / "41-release.pr-handoff.json"
    return json.loads(path.read_text(encoding="utf-8"))["facts"]


def _clean(runtime, worktree):
    git = runtime.git.at(worktree)
    return git.merge_head() is None and git.status().clean


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


def test_a_revert_push_that_keeps_meeting_server_errors_stays_in_acceptance_as_a_failure(
        kit, github_runtime, repos, worktree, documents_written, monkeypatch, simple_transitions):
    """验收中判定回归、推送撤销分支退避重试后仍遇到服务器错误：撤销 PR 还没提，Issue 留在验收中(不说成留在发布中)，
    这一步记为失败(不当成进展清零对象熔断计数)，下次运行再提撤销 PR、退回待修。"""
    _problem(github_runtime, kit, "P-0001", "collect.platform_errors")
    _accepting(kit, github_runtime, worktree, ["P-0001"], merged_at="2026-10-08T01:00:00Z")
    occurrences.add(github_runtime.conn, occurrences.Occurrence("P-0001", kit.NOW + timedelta(minutes=5),
                                                                "collect.platform_errors"), github_runtime.clock)
    github_runtime.clock.advance(timedelta(minutes=10))
    reverts = []

    def revert(runtime, issue, merge_commit, reason, conventions, github):
        reverts.append(merge_commit)
        if len(reverts) == 1:
            raise TransientError("推送 hotfix/7-revert 遇到远程服务器或网络的临时错误，共试 3 次仍失败："
                                 "refs/heads/hotfix/7-revert:refs/heads/hotfix/7-revert "
                                 "[remote rejected] (Internal Server Error)")
        return Reverted("hotfix/7-revert", 190, "https://github.com/cty/sample/pull/190")

    monkeypatch.setattr(flow.revert, "revert", revert)
    outcome = release(github_runtime, "0007")
    assert (outcome.point, outcome.status, outcome.next_point) == ("release.accept", Status.FAILED, "release.accept")
    assert "临时错误" in outcome.summary and "撤销 PR 还没提，Issue 留在验收中" in outcome.summary
    assert "发布中" not in outcome.summary
    issue = issues.get(github_runtime.conn, "0007")
    assert issue.status == "accepting" and load_state(issue).revert is None
    assert documents_written == [] and simple_transitions == []
    again = release(github_runtime, "0007")  # 服务器恢复：下次运行提撤销 PR，再退回待修
    assert again.status == Status.FAILED and "pull/190" in again.summary
    assert reverts == ["m" * 40, "m" * 40] and simple_transitions == [("0007", "regress", None)]
    assert issues.get(github_runtime.conn, "0007").status == "todo"


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


def test_a_push_that_keeps_meeting_server_errors_waits_for_the_next_run(kit, github_runtime, fake_github, repos,
                                                                        worktree, documents_written,
                                                                        simple_transitions):
    """GitHub 偶发 `[remote rejected] (Internal Server Error)`：退避重试几次仍失败也不转为待决定(不是要同步 main)，
    记事件、留在发布中，下次运行再推送。"""
    _ready(kit, github_runtime, repos, worktree)
    ref = f"refs/heads/{kit.BRANCH}"
    fake_github.runner.replies[PUSH] = [Outcome(
        1, f"To https://github.com/o/r.git\n!\t{ref}:{ref}\t[remote rejected] (Internal Server Error)\nDone\n",
        "remote: Internal Server Error        \nremote: Request ID F164:237968:143DD4:205B2B:6ACFACA0        \n"
        "remote: Time 2026-10-14T16:24:03Z\nerror: failed to push some refs to 'https://github.com/o/r.git'\n",
        1, None, None)]
    outcome = release(github_runtime, "0007")
    assert (outcome.status, outcome.next_point) == (Status.PASSED, "release.pr")
    assert "临时错误，共试 3 次仍失败" in outcome.summary and "下次运行再推送" in outcome.summary
    assert "同步" not in outcome.summary
    assert sum(command.argv[:len(PUSH)] == PUSH for command in fake_github.runner.commands) == 3
    assert issues.get(github_runtime.conn, "0007").status == "releasing"  # 不转为待决定
    assert documents_written == [] and simple_transitions == [] and fake_github.calls == []
    del fake_github.runner.replies[PUSH]  # 服务器恢复：下次运行照常推送并走完
    assert release(github_runtime, "0007").next_point == "release.deploy"
    assert issues.get(github_runtime.conn, "0007").status == "accepting"
