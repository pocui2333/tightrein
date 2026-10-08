"""提交：检查没全过且没接受时停下；工作区与交付清单不一致时停下并列出差异；提交信息按约定拼出、不带署名；
已提交过的不再提交；做决定之后仓库变了(前置条件过期)不提交。"""

import pytest

from tightrein.protocol.git import Stale, resolve
from tightrein.protocol.git.format import attribution_lines
from tightrein.release.commit import DECISION_KEYS, commit, file_differences
from tightrein.release.record import ReleaseBlocked, parse_delivery
from tightrein.store.tables import issues


def _setup(kit, runtime, repos, worktree, changed, **overrides):
    repos.write(worktree, {path: "PAGE = 0\n" for path in changed})
    found = parse_delivery(kit.delivery_facts(worktree, changed, **overrides), "0007")
    return kit.new_issue(runtime), found, resolve(runtime.settings, repos.repo), runtime.git.at(worktree)


def test_commit_uses_the_project_format_without_attribution(kit, runtime, repos, worktree):
    issue, found, conventions, git = _setup(kit, runtime, repos, worktree, ["src/order.py"])
    head = commit(runtime, issue, found, conventions, git)
    message = repos.git(worktree, "log", "-1", "--format=%B")
    assert head == repos.head(worktree)
    assert message.splitlines()[0] == "fix: 订单页翻页从第一页开始"
    assert "页码从 0 开始算错" in message
    assert attribution_lines(message) == []
    assert commit(runtime, issue, found, conventions, git) is None  # 已经提交过：工作区干净


def test_failed_checks_stop_unless_accepted(kit, runtime, repos, worktree):
    failed = [{"name": "pytest", "passed": False, "detail": "1 failed"}]
    issue, found, conventions, git = _setup(kit, runtime, repos, worktree, ["src/order.py"], checks=failed)
    with pytest.raises(ReleaseBlocked, match="pytest"):
        commit(runtime, issue, found, conventions, git)
    accepted = parse_delivery(kit.delivery_facts(worktree, ["src/order.py"], checks=failed,
                                                 acceptedFindings=["pytest：与本修复无关"]), "0007")
    assert commit(runtime, issue, accepted, conventions, git) is not None
    # 用户接受的未通过项写进 Issue 历史(记录与索引)
    note = "带着用户接受的未通过项提交：pytest：与本修复无关"
    assert issue.extra["history"][-1]["note"] == note
    assert issues.get(runtime.conn, "0007").extra["history"][-1]["note"] == note
    assert commit(runtime, issue, accepted, conventions, git) is None  # 已提交过：不再重复记
    assert len(issue.extra["history"]) == 1


def test_changes_after_delivery_stop_the_commit(kit, runtime, repos, worktree):
    issue, found, conventions, git = _setup(kit, runtime, repos, worktree, ["src/order.py"])
    repos.write(worktree, {"notes.tmp": "临时文件\n"})
    with pytest.raises(ReleaseBlocked, match="交付之后新增的改动：notes.tmp"):
        commit(runtime, issue, found, conventions, git)


def test_file_differences_are_sorted_and_explained():
    assert file_differences(("b", "c"), ("a", "b")) == ["交付之后新增的改动：c", "交付时改动过、现在没有改动：a"]


def test_the_commit_rechecks_the_state_seen_when_deciding(kit, runtime, repos, worktree, monkeypatch):
    issue, found, conventions, git = _setup(kit, runtime, repos, worktree, ["src/order.py"])
    seen = kit.stale_at_decision(monkeypatch, git)
    with pytest.raises(Stale, match="head"):
        commit(runtime, issue, found, conventions, git)
    assert set(seen[0]) == set(DECISION_KEYS)
    assert repos.git(worktree, "status", "--porcelain").strip()  # 没有提交
    assert commit(runtime, issue, found, conventions, git) is not None  # 下次重新观察后照常提交
