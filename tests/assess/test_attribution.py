from types import SimpleNamespace

from tightrein.assess.attribution import UNCOMMITTED, attribute
from tightrein.protocol.git import GitError


class FakeGitHub:
    def __init__(self, failing: set[str] = frozenset()) -> None:
        self.failing = failing

    def pr_for_commit(self, commit: str) -> SimpleNamespace | None:
        if commit in self.failing:
            raise GitError("gh api 403")
        return SimpleNamespace(number=12) if commit.startswith("a") else None


def test_attribution_lists_each_introducing_commit_and_explains_failures(kit):
    git = kit.FakeGit(None, blames={("a.py", 1): "a" * 40, ("a.py", 2): "a" * 40, ("b.py", 3): "b" * 40,
                                    ("c.py", 4): UNCOMMITTED, ("d.py", 5): "d" * 40})
    causes = [{"file": "a.py", "line": 1}, {"file": "a.py", "line": 2}, {"file": "b.py", "line": 3},
              {"file": "c.py", "line": 4}, {"file": "missing.py", "line": 9}, {"file": "d.py", "line": 5}]
    found = attribute(git, FakeGitHub(failing={"d" * 40}), kit.COMMIT, causes)
    # 第一处根因的提交为主引入提交；未提交的(全 0)与重复的去掉
    assert [(item.commit[0], item.pr) for item in found.introduced_by] == [("a", 12), ("b", None), ("d", None)]
    assert ("blame", ("a.py", 1, kit.COMMIT)) in git.calls  # 在取证 commit 上 blame
    assert any("missing.py:9 的 git blame 失败" in note for note in found.notes)
    assert any("PR 查询失败" in note for note in found.notes)
    assert found.introduced_by[0].to_json() == {"commit": "a" * 40, "author": "zhang", "pr": 12}


def test_without_github_no_pr_is_looked_up(kit):
    git = kit.FakeGit(None, blames={("a.py", 1): "a" * 40})
    assert attribute(git, None, kit.COMMIT, [{"file": "a.py", "line": 1}]).introduced_by[0].pr is None
