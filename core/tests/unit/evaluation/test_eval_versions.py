import os
import sqlite3
import stat

import pytest
from eval_world import EVALUATION

from tightrein.domain.enums import Stage
from tightrein.evaluation import versions
from tightrein.evaluation.errors import EvaluationRefused, SnapshotFailed
from tightrein.evaluation.variants import EvaluationPlan, Variant, VersionSpec, tool_model_plan, version_plan


def test_plans_check_repeats_labels_and_variants():
    base = VersionSpec("baseline")
    candidate = VersionSpec("IP-0003", patch=None, use_worktree=True)
    plan = version_plan(Stage.TRIAGE, candidate, "claude", "claude-opus", ("E-0001",))
    assert [variant.label for variant in plan.variants] == ["baseline", "IP-0003"]
    assert plan.baseline.version == base and list(plan.versions) == ["baseline", "IP-0003"]
    assert EvaluationPlan.from_dict(plan.to_dict()) == plan
    tools = tool_model_plan(Stage.FIX, ["claude", "codex"], ["opus", None])
    assert [variant.label for variant in tools.variants] == ["claude+opus", "claude+default", "codex+opus",
                                                             "codex+default"]
    assert list(tools.versions) == ["baseline"] and tools.purpose == "tool-model"
    with pytest.raises(ValueError, match="至少运行 3 次"):
        version_plan(Stage.TRIAGE, candidate, "claude", repeats=2)
    with pytest.raises(ValueError, match="变体标签重复"):
        EvaluationPlan(Stage.TRIAGE, (), (Variant("a", base, "claude"), Variant("a", base, "codex")))
    with pytest.raises(ValueError, match="基线与至少一个候选"):
        EvaluationPlan(Stage.TRIAGE, (), (Variant("a", base, "claude"),))
    with pytest.raises(ValueError, match="路径段"):
        EvaluationPlan(Stage.TRIAGE, (), (Variant("a/b", base, "claude"),), purpose="tool-model")


def commit_files(world, files, message="feat: 改动"):
    world.repos.commit(world.tool.root, message, files)
    return world.repos.head(world.tool.root)


def patch_for(world, tmp_path, files):
    """在工作区改动这些文件，导出 diff 后还原。"""
    root = world.tool.root
    for path, text in files.items():
        world.repos.write(root, path, text)
    patch = tmp_path / "proposal.patch"
    patch.write_text(world.repos.git(root, "diff"), encoding="utf-8")
    world.repos.git(root, "checkout", "--", ".")
    return patch


def snapshot_dir(world, label):
    return world.layout.eval_version_dir(EVALUATION, label)


def test_commit_and_patch_snapshots(world, tmp_path):
    first = commit_files(world, {"skills/triage/SKILL.md": "旧说明\n", "core/tightrein/x.py": "A = 1\n"})
    commit_files(world, {"skills/triage/SKILL.md": "新说明\n"})
    old = versions.build_snapshot(VersionSpec("old", first), world.tool.root, snapshot_dir(world, "old"), world.process)
    assert (old / "skills/triage/SKILL.md").read_text(encoding="utf-8") == "旧说明\n"
    patch = patch_for(world, tmp_path, {"skills/triage/SKILL.md": "提案说明\n"})
    candidate = versions.build_snapshot(VersionSpec("IP-0003", patch=patch), world.tool.root,
                                        snapshot_dir(world, "IP-0003"), world.process)
    assert (candidate / "skills/triage/SKILL.md").read_text(encoding="utf-8") == "提案说明\n"
    assert (world.tool.root / "skills/triage/SKILL.md").read_text(encoding="utf-8") == "新说明\n"
    assert versions.changed_files(old, candidate) == ["skills/triage/SKILL.md"]
    stale = patch_for(world, tmp_path, {"core/tightrein/x.py": "A = 2\n"})
    commit_files(world, {"core/tightrein/x.py": "A = 3\n"})
    with pytest.raises(SnapshotFailed, match="不能干净地应用"):
        versions.build_snapshot(VersionSpec("IP-0004", patch=stale), world.tool.root, snapshot_dir(world, "IP-0004"),
                                world.process)
    with pytest.raises(SnapshotFailed, match="无法导出"):
        versions.build_snapshot(VersionSpec("bad", "0" * 40), world.tool.root, snapshot_dir(world, "bad"),
                                world.process)


def test_worktree_snapshots_skip_ignored_files(world):
    commit_files(world, {"skills/triage/SKILL.md": "说明\n"})
    world.repos.write(world.tool.root, "skills/triage/draft.md", "未提交\n")
    snapshot = versions.build_snapshot(VersionSpec("work", use_worktree=True), world.tool.root,
                                       snapshot_dir(world, "work"), world.process)
    assert (snapshot / "skills/triage/draft.md").is_file()
    assert not (snapshot / "workspaces/sample/data").exists()


@pytest.mark.parametrize("path", ["core/tightrein/evaluation/stats.py", "core/tightrein/guards/policy.py",
                                  "core/tightrein/pipeline/improve/service.py", "skills/improve/SKILL.md",
                                  "workspaces/sample/evals/triage/E-0001/case.json"])
def test_candidates_touching_protected_paths_are_refused(world, tmp_path, path):
    commit_files(world, {path: "原内容\n", "skills/triage/SKILL.md": "说明\n"})
    baseline = versions.build_snapshot(VersionSpec("baseline"), world.tool.root, snapshot_dir(world, "baseline"),
                                       world.process)
    allowed = versions.build_snapshot(
        VersionSpec("allowed", patch=patch_for(world, tmp_path, {"skills/triage/SKILL.md": "新说明\n"})),
        world.tool.root, snapshot_dir(world, "allowed"), world.process)
    versions.check_candidates(baseline, [allowed])
    touched = versions.build_snapshot(VersionSpec("touched", patch=patch_for(world, tmp_path, {path: "改过\n"})),
                                      world.tool.root, snapshot_dir(world, "touched"), world.process)
    with pytest.raises(EvaluationRefused) as raised:
        versions.check_candidates(baseline, [allowed, touched])
    assert raised.value.paths == (path,)


def test_database_copies_match_and_are_independent(world):
    world.conn.execute("INSERT INTO sequences (name, value) VALUES ('problem', 41)")
    commit_files(world, {"skills/triage/SKILL.md": "说明\n"})
    built = versions.build_versions(
        {"baseline": VersionSpec("baseline"), "work": VersionSpec("work", use_worktree=True)}, world.tool.root,
        {"baseline": snapshot_dir(world, "baseline"), "work": snapshot_dir(world, "work")}, world.process,
        world.layout.database(), "sample")
    copies = [versions.workspace_database(path, "sample") for path in built.values()]
    for copy in copies:
        with sqlite3.connect(copy) as conn:
            assert conn.execute("SELECT value FROM sequences WHERE name = 'problem'").fetchone() == (41,)
    with sqlite3.connect(copies[0]) as conn:
        conn.execute("UPDATE sequences SET value = 99 WHERE name = 'problem'")
    assert world.conn.execute("SELECT value FROM sequences WHERE name = 'problem'").fetchone()[0] == 41
    with pytest.raises(SnapshotFailed, match="不存在"):
        versions.copy_database(world.layout.root / "none.db", copies)


def test_project_snapshots_are_read_only(world, repos):
    _, project = repos.origin_and_clone()
    commit = repos.head(project)
    snapshot = versions.export_project(world.process, project, commit,
                                       world.layout.eval_project_snapshot(EVALUATION, commit))
    source = snapshot / "src" / "OrderService.cs"
    assert source.read_text(encoding="utf-8").startswith("class OrderService")
    assert not os.access(source, os.W_OK) and not stat.S_IMODE(os.stat(snapshot).st_mode) & stat.S_IWUSR
    assert versions.export_project(world.process, project, commit, snapshot) == snapshot
    for directory, _, _ in os.walk(snapshot):
        os.chmod(directory, 0o755)
