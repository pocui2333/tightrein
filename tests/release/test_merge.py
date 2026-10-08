"""自动合并：全部条件满足才合并(带 --match-head-commit)；评审只看每人最后一次结论；PR 头部要等于本工具最近一次
推送的 commit；高风险路径一律交人且同一组只写一次；原因没变不重复记；要求必需检查时开 GitHub 原生自动合并，
同一 head 只开一次；免费私有仓库查规则集 403 视为没有。"""

import json

from tightrein.protocol.git.github import MergeFacts
from tightrein.protocol.process import Outcome
from tightrein.release import merge
from tightrein.release.ci import CiState
from tightrein.release.record import ReleaseState, parse_delivery

HEAD = "a" * 40
PASSED = CiState("passed")


def _ready(kit, fake_github, tmp_path, **facts):
    number = fake_github.create_pr(kit.BRANCH, "main", "t", "b", scope=None)
    fake_github.pulls[number]["head"] = HEAD
    return number, parse_delivery(kit.delivery_facts(tmp_path, ["src/order.py"], **facts), "0007"), \
        ReleaseState(pushed=HEAD)


def _events(runtime):
    path = runtime.workspace.events(runtime.run)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_only_the_last_review_of_each_reviewer_counts():
    states = [("li", "CHANGES_REQUESTED"), ("li", "COMMENTED"), ("wang", "CHANGES_REQUESTED"), ("wang", "APPROVED"),
              ("zhao", "APPROVED"), ("zhao", "CHANGES_REQUESTED")]
    reviews = [{"author": {"login": login}, "state": state} for login, state in states]
    assert merge.changes_requested(reviews) == ["li", "zhao"]


def test_blockers_list_every_reason(kit, tmp_path):
    found = parse_delivery(kit.delivery_facts(tmp_path, ["a"], acceptedFindings=["lint"]), "0007")
    facts = MergeFacts("OPEN", True, "CONFLICTING", "DIRTY", "b" * 40,
                       ({"author": {"login": "li"}, "state": "CHANGES_REQUESTED"},))
    reasons = merge.blockers(found, ReleaseState(pushed=HEAD), facts, CiState("failed", ("test",)))
    assert reasons == ["最后一轮检查没有全部通过，或接受了未通过项", "PR 是草稿", "li 请求修改",
                       "PR 头部 commit 不是本工具最近一次推送的 commit", "PR 与主分支冲突", "与主分支冲突",
                       "CI 检查未通过：test"]


def test_merging_when_every_condition_holds(kit, github_runtime, fake_github, tmp_path):
    number, found, state = _ready(kit, fake_github, tmp_path)
    decision = merge.decide(github_runtime, "0007", number, found, state, PASSED, fake_github)
    assert decision.merged and not decision.native
    assert fake_github.calls[-1] == ("pr-merge", number, HEAD, "squash", False)


def test_unmet_conditions_are_recorded_only_when_they_change(kit, github_runtime, fake_github, tmp_path):
    number, found, state = _ready(kit, fake_github, tmp_path)
    state.pushed = "c" * 40  # PR 上有别人的提交
    for _ in range(2):
        decision = merge.decide(github_runtime, "0007", number, found, state, PASSED, fake_github)
        assert not decision.merged and decision.gate is None
    assert decision.reasons == ["PR 头部 commit 不是本工具最近一次推送的 commit"]
    assert len([event for event in _events(github_runtime) if event["summary"].startswith("未自动合并")]) == 1
    assert not any(call[0] == "pr-merge" for call in fake_github.calls)


def test_high_risk_paths_go_to_a_person_once(kit, github_runtime, fake_github, tmp_path):
    number, _, state = _ready(kit, fake_github, tmp_path)
    found = parse_delivery(kit.delivery_facts(tmp_path, ["src/order.py", ".github/workflows/ci.yml"]), "0007")
    first = merge.decide(github_runtime, "0007", number, found, state, PASSED, fake_github)
    again = merge.decide(github_runtime, "0007", number, found, state, PASSED, fake_github)
    assert first.gate == merge.HIGH_RISK_GATE and first.new_gate and ".github/workflows/ci.yml" in first.gate_reason
    assert again.gate == merge.HIGH_RISK_GATE and not again.new_gate
    assert not any(call[0] == "pr-merge" for call in fake_github.calls)


def test_a_manual_merge_gate_goes_to_a_person(kit, tmp_path, repos, fake_github):
    runtime = kit.make_runtime(tmp_path, repos.repo, github=fake_github,
                               settings=kit.make_settings({"boundaries": {"gates": {"merge": "manual"}}}))
    number, found, state = _ready(kit, fake_github, tmp_path)
    assert merge.decide(runtime, "0007", number, found, state, PASSED, fake_github).gate == merge.MERGE_GATE


def test_native_auto_merge_is_enabled_once_per_head(kit, github_runtime, fake_github, tmp_path):
    fake_github.required = True
    number, found, state = _ready(kit, fake_github, tmp_path)
    pending = CiState("pending", (), ("build",))
    first = merge.decide(github_runtime, "0007", number, found, state, pending, fake_github)
    again = merge.decide(github_runtime, "0007", number, found, state, pending, fake_github)
    assert first.native and again.native and not first.merged
    assert [call for call in fake_github.calls if call[0] == "pr-merge"] == [("pr-merge", number, HEAD, "squash", True)]
    failed = merge.decide(github_runtime, "0007", number, found, state, CiState("failed", ("build",)), fake_github)
    assert not failed.native and failed.reasons == ["CI 检查未通过：build"]


def test_a_merge_queue_hands_merging_to_github(kit, github_runtime, fake_github):
    fake_github.runner.replies[("gh", "api")] = [Outcome(0, json.dumps([{"type": "merge_queue"}]), "", 1, None, None)]
    assert merge.native(github_runtime, fake_github)


def test_rulesets_forbidden_on_free_private_repos_mean_none(kit, github_runtime, fake_github):
    fake_github.runner.replies[("gh", "api")] = [Outcome(1, "", "gh: Upgrade to GitHub Pro (HTTP 403)", 1, None, None)]
    assert not merge.merge_queue(github_runtime, fake_github)
