"""自动合并：全部条件满足才合并(带 --match-head-commit)；评审只看每人最后一次结论；PR 头部要等于本工具最近一次
推送的 commit；高风险路径交人且同一组只写一次，定案时用户逐项确认过的照常自动合并；用户在合并关卡上通过后由本工具
合并(选自己在 GitHub 上合并的不合并，重新交付后要重新通过)；原因没变不重复记；要求必需检查时开 GitHub 原生自动合并，
同一 head 只开一次；免费私有仓库查规则集 403 视为没有。"""

import json

import pytest

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


PLAN = "p" * 64


def _plan(runtime, touches, approved_by="user", plan_hash=PLAN):
    """落一份方案与定案的交接：方案要改的受保护文件为 touches，定案由 approved_by 通过、确认的方案为 plan_hash。"""
    from tightrein.protocol import handoff
    from tightrein.protocol.handoff import Handoff, Status
    from tightrein.protocol.naming import FileName

    at = "2026-10-08T02:00:00Z"
    design = {"planHash": PLAN, "protectedTouches": [{"path": path, "change": "追加一行", "reason": "要用"}
                                                     for path in touches]}
    approve = {"planHash": plan_hash, "auto": approved_by == "auto", "approvedBy": approved_by,
               "note": "同意在 requirements.txt 加 fastembed>=0.4.0"}
    for point, facts in (("implement.design", design), ("implement.approve", approve)):
        path = runtime.workspace.step_file("0007", FileName(point, "handoff", "json"))
        handoff.write(path, Handoff(point, "0007", runtime.run, Status.PASSED, "通过", facts, created_at=at))


def test_high_risk_paths_the_user_approved_at_design_merge_automatically(kit, github_runtime, fake_github, tmp_path):
    number, _, state = _ready(kit, fake_github, tmp_path)
    _plan(github_runtime, ["requirements.txt"])
    found = parse_delivery(kit.delivery_facts(tmp_path, ["src/order.py", "requirements.txt"],
                                              highRiskPaths=["requirements.txt"]), "0007")
    decision = merge.decide(github_runtime, "0007", number, found, state, PASSED, fake_github)
    assert decision.merged and decision.gate is None
    assert fake_github.calls[-1] == ("pr-merge", number, HEAD, "squash", False)
    approval = decision.approvals[0].to_json()
    assert approval["point"] == "implement.approve" and approval["paths"] == ["requirements.txt"]
    assert approval["planHash"] == PLAN and approval["at"] == "2026-10-08T02:00:00Z"
    merged = [event["summary"] for event in _events(github_runtime) if event["summary"].startswith("已合并")]
    assert merged and "requirements.txt 已在定案时由用户确认" in merged[0]


@pytest.mark.parametrize(("touches", "approved_by", "plan_hash", "gated"), [
    (["requirements.txt"], "user", PLAN, ".github/workflows/ci.yml(定案时只确认了 requirements.txt)"),  # 多碰了没列过的
    (["package.json"], "user", PLAN, ".github/workflows/ci.yml、requirements.txt"),  # 列的是别的文件
    ([".github/workflows/ci.yml", "requirements.txt"], "auto", PLAN, ".github/workflows/ci.yml、requirements.txt"),
    ([".github/workflows/ci.yml", "requirements.txt"], "user", "q" * 64, ".github/workflows/ci.yml、requirements.txt"),
])
def test_high_risk_paths_the_user_did_not_approve_still_go_to_a_person(kit, github_runtime, fake_github, tmp_path,
                                                                       touches, approved_by, plan_hash, gated):
    number, _, state = _ready(kit, fake_github, tmp_path)
    _plan(github_runtime, touches, approved_by, plan_hash)
    found = parse_delivery(kit.delivery_facts(tmp_path, ["requirements.txt", ".github/workflows/ci.yml"]), "0007")
    decision = merge.decide(github_runtime, "0007", number, found, state, PASSED, fake_github)
    assert decision.gate == merge.HIGH_RISK_GATE and decision.gate_reason == f"改动涉及高风险路径，不自动合并：{gated}"
    assert not decision.merged and not any(call[0] == "pr-merge" for call in fake_github.calls)


def _approved(at, option=None, verdict="approve", point=merge.HIGH_RISK_GATE):
    return {"point": point, "verdict": verdict, "option": option, "note": "看过了", "at": at}


def test_approving_the_gate_lets_the_tool_merge(kit, github_runtime, fake_github, tmp_path):
    number, _, state = _ready(kit, fake_github, tmp_path)
    found = parse_delivery(kit.delivery_facts(tmp_path, ["requirements.txt"]), "0007")
    old = [_approved("2026-10-08T02:59:59Z")]  # 关卡出现之前的通过不算
    for _ in range(2):
        gated = merge.decide(github_runtime, "0007", number, found, state, PASSED, fake_github, old)
        assert gated.gate == merge.HIGH_RISK_GATE and state.gate_at == "2026-10-08T03:00:00Z"
    rejected = [*old, _approved("2026-10-08T03:05:00Z", verdict="reject")]
    again = merge.decide(github_runtime, "0007", number, found, state, PASSED, fake_github, rejected)
    assert again.gate == merge.HIGH_RISK_GATE and not again.new_gate
    assert not any(call[0] == "pr-merge" for call in fake_github.calls)

    approved = [*rejected, _approved("2026-10-08T03:10:00Z", option=1)]
    decision = merge.decide(github_runtime, "0007", number, found, state, PASSED, fake_github, approved)
    assert decision.merged and decision.gate is None
    assert fake_github.calls[-1] == ("pr-merge", number, HEAD, "squash", False)
    assert [item.to_json() for item in decision.approvals] == [
        {"point": "high_risk_merge", "at": "2026-10-08T03:10:00Z", "note": "看过了", "option": 1, "planHash": None,
         "paths": []}]


def test_choosing_to_merge_on_github_leaves_the_pr_to_the_user(kit, github_runtime, fake_github, tmp_path):
    number, _, state = _ready(kit, fake_github, tmp_path)
    found = parse_delivery(kit.delivery_facts(tmp_path, ["requirements.txt"]), "0007")
    merge.decide(github_runtime, "0007", number, found, state, PASSED, fake_github)
    chosen = [_approved("2026-10-08T03:10:00Z", option=merge.SELF_MERGE)]
    decision = merge.decide(github_runtime, "0007", number, found, state, PASSED, fake_github, chosen)
    assert not decision.merged and decision.gate is None and "本工具不合并" in decision.reasons[0]
    assert not any(call[0] == "pr-merge" for call in fake_github.calls)


def test_a_new_delivery_needs_the_gate_approved_again(kit, github_runtime, fake_github, tmp_path):
    number, _, state = _ready(kit, fake_github, tmp_path)
    found = parse_delivery(kit.delivery_facts(tmp_path, ["requirements.txt"]), "0007")
    merge.decide(github_runtime, "0007", number, found, state, PASSED, fake_github)
    approved = [_approved("2026-10-08T03:00:00Z")]
    changed = parse_delivery(kit.delivery_facts(tmp_path, ["requirements.txt"], diffHash="e" * 64), "0007")
    decision = merge.decide(github_runtime, "0007", number, changed, state, PASSED, fake_github, approved)
    assert decision.gate == merge.HIGH_RISK_GATE and decision.new_gate
    assert not any(call[0] == "pr-merge" for call in fake_github.calls)


def test_approving_a_manual_merge_gate_lets_the_tool_merge(kit, tmp_path, repos, fake_github):
    runtime = kit.make_runtime(tmp_path, repos.repo, github=fake_github,
                               settings=kit.make_settings({"boundaries": {"gates": {"merge": "manual"}}}))
    number, found, state = _ready(kit, fake_github, tmp_path)
    assert merge.decide(runtime, "0007", number, found, state, PASSED, fake_github).gate == merge.MERGE_GATE
    approved = [_approved("2026-10-08T03:00:00Z", point=merge.MERGE_GATE)]
    assert merge.decide(runtime, "0007", number, found, state, PASSED, fake_github, approved).merged
