"""提 PR：描述只有问题、改了什么、检查结果(含接受的未通过项)与 `Closes #<镜像>`；分支名不合规时停下并给建议名；
PR 先查后做、描述没变不更新、做决定时看到的 PR 作为 expected 交给新建；审查结论同一 PR 同一轮只发一条评论。"""

from dataclasses import replace

import pytest

from tightrein.protocol.git import resolve
from tightrein.protocol.git.format import attribution_lines
from tightrein.release.pr import LABELS, mirror_number, open_pull, post_review, pull_text, suggest
from tightrein.release.record import ReleaseBlocked, ReleaseState, parse_delivery
from tightrein.store.tables.issues import Issue


def test_the_body_is_short_and_closes_the_mirror(kit, runtime, tmp_path):
    issue = kit.new_issue(runtime, extra={"github": {"number": 42}})
    facts = kit.delivery_facts(tmp_path, ["src/order.py"], acceptedFindings=["lint：行太长"],
                               checks=[{"name": "pytest", "passed": True},
                                       {"name": "lint", "passed": False, "detail": "E501"}])
    body = pull_text(issue, parse_delivery(facts, "0007"), resolve(runtime.settings, tmp_path), LABELS["zh"])
    assert body.startswith("订单页翻到第二页时显示第一页")
    assert "Closes #42" in body
    assert "- pytest：通过" in body and "- lint：未通过(E501)" in body and "- 接受的未通过项：lint：行太长" in body
    assert attribution_lines(body) == []


def test_a_project_template_receives_the_same_content(kit, runtime, repos, tmp_path):
    repos.write(repos.repo, {".github/pull_request_template.md": "## Summary\n\n<!-- what -->\n\n## Testing\n\n"
                                                                 "- [ ] tests added\n"})
    issue = kit.new_issue(runtime)
    body = pull_text(issue, parse_delivery(kit.delivery_facts(tmp_path, ["src/order.py"]), "0007"),
                     resolve(runtime.settings, repos.repo), LABELS["zh"])
    assert "## Summary\n\n订单页翻到第二页时显示第一页" in body
    assert "- pytest：通过" in body and "- [ ] tests added" in body


def test_mirror_numbers():
    def issue(value):
        return Issue("0007", "releasing", "t", "bug", "problem", extra={"github": value} if value else {})

    assert mirror_number(issue(42)) == 42 and mirror_number(issue({"number": "7"})) == 7
    assert mirror_number(issue(None)) is None


def test_a_bad_branch_name_stops_with_a_suggestion(kit, github_runtime, fake_github, tmp_path):
    issue = kit.new_issue(github_runtime)
    found = parse_delivery(kit.delivery_facts(tmp_path, ["src/order.py"], branch="claude/fix-order"), "0007")
    with pytest.raises(ReleaseBlocked, match="建议改名为 fix/7-"):
        open_pull(github_runtime, issue, found, resolve(github_runtime.settings, tmp_path), fake_github)
    assert fake_github.pulls == {}


def test_suggestions_leave_the_personal_prefix_to_the_user(kit, runtime, tmp_path):
    conventions = resolve(runtime.settings, tmp_path)
    issue = kit.new_issue(runtime, severity="P0")
    assert suggest(issue, replace(conventions, personal_prefix=True, prefix="claude")).startswith("<个人前缀>/hotfix/7-")


def test_the_pr_is_opened_once_and_updated_only_when_the_body_changes(kit, github_runtime, fake_github, tmp_path):
    issue = kit.new_issue(github_runtime)
    found = parse_delivery(kit.delivery_facts(tmp_path, ["src/order.py"]), "0007")
    conventions = resolve(github_runtime.settings, tmp_path)
    first = open_pull(github_runtime, issue, found, conventions, fake_github)
    again = open_pull(github_runtime, issue, found, conventions, fake_github)
    assert first.number == again.number == 186
    assert first.title == "订单页翻页从第一页开始"
    assert [call[0] for call in fake_github.calls] == ["pr-create"]


def test_the_review_summary_is_posted_once_per_round(kit, github_runtime, fake_github, tmp_path):
    issue = kit.new_issue(github_runtime)
    found = parse_delivery(kit.delivery_facts(tmp_path, ["src/order.py"]), "0007")
    pull = open_pull(github_runtime, issue, found, resolve(github_runtime.settings, tmp_path), fake_github)
    state = ReleaseState()
    assert post_review(github_runtime, "0007", pull.number, found, state, fake_github)
    assert not post_review(github_runtime, "0007", pull.number, found, state, fake_github)
    comments = fake_github.pulls[pull.number]["comments"]
    assert len(comments) == 1 and "不是批准" in comments[0] and "light：通过" in comments[0]
    second = parse_delivery(kit.delivery_facts(tmp_path, ["src/order.py"], review={"round": 2, "conclusions": []}),
                            "0007")
    assert post_review(github_runtime, "0007", pull.number, second, state, fake_github)


def test_the_open_pull_seen_when_deciding_is_passed_as_expected(kit, github_runtime, fake_github, tmp_path):
    issue = kit.new_issue(github_runtime)
    found = parse_delivery(kit.delivery_facts(tmp_path, ["src/order.py"]), "0007")
    conventions = resolve(github_runtime.settings, tmp_path)
    open_pull(github_runtime, issue, found, conventions, fake_github)
    open_pull(github_runtime, issue, found, conventions, fake_github)
    assert fake_github.expected == [{"pullRequest": None}, {"pullRequest": "186"}]
