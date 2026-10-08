"""清理：worktree 干净时删除目录与已合并的本地分支并 fetch --prune；有未提交内容时保留现场；git 认不出已合并时
不用 -D，保留分支并说明；重复执行不出错。"""

from tightrein.release.cleanup import DONE, cleanup


def test_a_clean_merged_fix_is_removed(kit, runtime, repos, worktree):
    repos.commit(worktree, "fix: page", {"src/order.py": "PAGE = 0\n"})
    repos.git(worktree, "push", "-q", "-u", "origin", kit.BRANCH)  # 推送过：上游与本地一致，branch -d 认得出已合并
    assert cleanup(runtime, "0007", worktree, kit.BRANCH) == DONE
    assert not worktree.exists()
    assert kit.BRANCH not in repos.git(repos.repo, "branch", "--list", kit.BRANCH)
    assert cleanup(runtime, "0007", worktree, kit.BRANCH) == DONE


def test_uncommitted_work_is_kept(kit, runtime, repos, worktree):
    repos.write(worktree, {"src/order.py": "PAGE = 0\n"})
    kept = cleanup(runtime, "0007", worktree, kit.BRANCH)
    assert "保留现场" in kept and "src/order.py" in kept
    assert worktree.exists()


def test_an_unmerged_branch_is_not_force_deleted(kit, runtime, repos, worktree):
    repos.commit(worktree, "fix: page", {"src/order.py": "PAGE = 0\n"})  # 没推送、没合并
    kept = cleanup(runtime, "0007", worktree, kit.BRANCH)
    assert "不用 -D" in kept
    assert not worktree.exists()
    assert kit.BRANCH in repos.git(repos.repo, "branch", "--list", kit.BRANCH)
