"""收件箱(redesign/09-loop.md 第 4 节)：每件附推荐做法；决策简报、CI 失败与接入问题也列出。"""

from pipeline_world import NOW, make_world, save_issue

from tightrein.domain.enums import HandoffStatus, IssueStatus, RunStage, SuggestionKind, SuggestionStatus
from tightrein.orchestrator import inbox
from tightrein.pipeline.common import stage_runs
from tightrein.store.repos import onboarding_items, suggestions
from tightrein.store.repos.onboarding_items import OnboardingItem
from tightrein.store.repos.suggestions import SuggestionRecord


def test_pending_merges_with_a_decision_brief_or_failed_ci_are_listed(tmp_path):
    world = make_world(tmp_path)
    save_issue(world.conn, "0007", IssueStatus.PENDING_MERGE)
    run = stage_runs.begin(RunStage.RELEASE, world.layout, world.conn, world.clock, world.events)
    run.handoff(RunStage.RELEASE, "0007", HandoffStatus.OK, {
        "issueId": "0007", "branch": "bugfix/7-x", "commits": [], "syncs": [], "deployments": [],
        "pendingOperations": [], "autoMerge": {
            "merged": False, "reasons": ["CI 必需检查未通过：build"], "operationId": None, "at": "2026-10-05T03:00:00Z",
            "decision": "data/fixes/0007/merge-decision.md",
            "ci": {"state": "failed", "failed": ["build"], "pending": []}}}, "继续跟踪")
    found = inbox.items(world.conn, world.layout)
    assert [item["kind"] for item in found] == ["pr-review", "merge-decision", "ci-failed"]
    assert all(item["recommendation"] for item in found)
    assert "build" in found[2]["summary"] and inbox.items(world.conn) == found[:1]


def test_onboarding_questions_come_with_the_recommended_answer(tmp_path):
    world = make_world(tmp_path)
    onboarding_items.save(world.conn, OnboardingItem("platform:deploy-source", 3, "平台接入：部署来源", "blocked", "user",
                                                     "没有配置", NOW, "用 GitHub Actions 的部署工作流 deploy.yml 跟踪部署"))
    [item] = inbox.items(world.conn)
    assert (item["kind"], item["command"]) == ("onboarding", "tightrein workspace answer platform:deploy-source --recommended")
    assert item["recommendation"].startswith("用 GitHub Actions")


def test_failed_onboarding_checks_are_listed_with_their_cause(tmp_path):
    world = make_world(tmp_path)
    onboarding_items.save(world.conn, OnboardingItem("checks", 1, "检查命令在基准版本上通过", "failed", "system",
                                                     "主分支上失败：无法把只读 worktree 切到主分支(先执行 tightrein worktree init)",
                                                     NOW))
    [item] = inbox.items(world.conn)
    assert (item["kind"], item["command"]) == ("onboarding", "tightrein workspace check")
    assert "先执行 tightrein worktree init" in item["summary"]


def test_pending_learn_suggestions_carry_their_document_and_advice(tmp_path):
    world = make_world(tmp_path)
    suggestions.save(world.conn, SuggestionRecord(
        "LS-0003", SuggestionKind.CONTROL, "gate:merge", SuggestionStatus.PENDING, NOW,
        {"advice": {"recommendation": "把 gates.merge 改为 user", "reason": "本周撤销合并 3 次"}},
        target_path="data/improve/LS-0003.md"))
    suggestions.save(world.conn, SuggestionRecord("LS-0004", SuggestionKind.COVERAGE_GAP, "api-fuzz",
                                                  SuggestionStatus.ACCEPTED, NOW, {}))
    [item] = inbox.items(world.conn)
    assert (item["kind"], item["subjectId"], item["command"]) == (
        "learn-suggestion", "LS-0003", "tightrein learn accept LS-0003")
    assert "data/improve/LS-0003.md" in item["summary"] and item["recommendation"] == "把 gates.merge 改为 user"
