import os
from types import SimpleNamespace

import pytest

from tightrein.agents.result import CallStatus
from tightrein.collect.common.source import SourceStatus, SourceUnavailable
from tightrein.collect.static import source
from tightrein.collect.static.claims import ToolFinding
from tightrein.collect.static.scope import Level
from tightrein.collect.static.tools import semgrep
from tightrein.protocol.git import worktrees
from tightrein.store.tables import state


def save(runtime, result):
    """去重在写信号的同一个事务里保存 state；测试里直接写。"""
    for key, value in result.state.items():
        state.put(runtime.conn, key, value, runtime.clock)


def worktree_of(runtime):
    return runtime.workspace.worktree(source.WORKTREE)


def test_the_first_run_is_a_baseline_and_confirmed_claims_become_signals(static_runtime, fake_caller, make_claim,
                                                                         make_verdict, repos):
    runtime = static_runtime()
    caller = fake_caller({
        "collect.static.baseline": {"claims": [make_claim(layer="baseline"),
                                               make_claim(line=8, rule="r-low", severity="low", layer="baseline")],
                                    "excluded": []},
        "collect.static.verify": make_verdict(),
    })
    result = source.collect(runtime, caller=caller)
    head = repos.git(repos.repo, "rev-parse", "HEAD").strip()
    assert result.status is SourceStatus.DONE and caller.points() == ["collect.static.baseline",
                                                                      "collect.static.verify"]
    assert [(item.location, item.symbol, item.verified, item.deterministic) for item in result.signals] == [
        ("src/orders.py:3", "list_orders", True, True)]
    assert result.signals[0].evidence["introducedBy"]["commit"] == head
    assert result.state[source.STATE_KEY]["commit"] == head and result.state[source.STATE_KEY]["level"] == "baseline"
    assert set(result.state[source.REVIEWED_KEY]) == {"README.md", "src/orders.py", "tests/test_orders.py"}
    assert [(item["claim"]["line"], item["reason"]) for item in result.state[source.PENDING_KEY]] == [(8, "low")]
    assert result.coverage == ["README.md", "src/orders.py", "tests/test_orders.py"] and result.read == 3
    assert result.metrics.produced["signals"] == 1 and result.metrics.calls == 2
    assert any("第一次巡检" in note for note in result.notes)
    # 审查期间去掉的写权限已恢复，标记已删
    assert os.access(worktree_of(runtime) / "src" / "orders.py", os.W_OK)
    assert not list(runtime.workspace.worktrees_dir.glob("readonly-*.json"))
    save(runtime, result)
    assert source.collect(runtime, caller=caller).status is SourceStatus.SKIPPED


def test_increments_review_only_code_changes_with_the_prepared_function(static_runtime, fake_caller, make_claim,
                                                                        make_verdict, repos, service_text):
    runtime = static_runtime(controls={"collect.static.review": {"maxClaims": 2}})
    first = source.collect(runtime, caller=fake_caller({"collect.static.baseline": {"claims": [], "excluded": []}}))
    save(runtime, first)
    repos.push("docs: 说明", {"README.md": "# demo\n\n更多说明\n"})
    docs = fake_caller({})
    result = source.collect(runtime, caller=docs)
    assert docs.calls == [] and source.REVIEW_SKIPPED in result.notes and result.status is SourceStatus.DONE
    save(runtime, result)
    repos.push("feat: 改页大小", {"src/orders.py": service_text.replace("size = 20", "size = 50")})
    caller = fake_caller({
        "collect.static.review": {"claims": [make_claim(line=2, severity="medium"), make_claim(line=3, rule="r-high"),
                                             make_claim(line=1, rule="r-low", severity="low"),
                                             make_claim(file="src/none.py", rule="r-x", severity="low")],
                                  "excluded": []},
        "collect.static.verify": make_verdict("refuted"),
    })
    result = source.collect(runtime, caller=caller)
    review = caller.calls[0]
    assert review.point == "collect.static.review" and "+    size = 50" in review.prompt
    assert "    1  def list_orders(page):" in review.prompt and "## 主张条数上限\n\n2" in review.prompt
    # 按严重度只留前 2 条：高、中；refuted 的不产出信号
    assert [params.point for params in caller.calls] == ["collect.static.review"] + ["collect.static.verify"] * 2
    assert result.signals == [] and result.metrics.produced["refutedClaims"] == 2
    assert any("截掉 2 条" in note for note in result.notes)
    assert result.state[source.STATE_KEY]["level"] == "incremental" and result.coverage == ["src/orders.py"]


def test_an_environment_violation_voids_the_whole_run(static_runtime, fake_caller, make_failed):
    runtime = static_runtime()
    caller = fake_caller({"collect.static.baseline": make_failed(
        CallStatus.BOUNDARY, ("read_only_changed：worktree 在调用前后有变化",))})
    with pytest.raises(SourceUnavailable, match="整次巡检作废"):
        source.collect(runtime, caller=caller)
    assert os.access(worktree_of(runtime) / "src" / "orders.py", os.W_OK)
    assert not list(runtime.workspace.worktrees_dir.glob("readonly-*.json"))


def test_a_failed_review_keeps_the_start_and_an_exhausted_budget_keeps_the_claims(static_runtime, fake_caller,
                                                                                  make_claim, make_failed):
    runtime = static_runtime()
    caller = fake_caller({"collect.static.baseline": {"claims": [make_claim(layer="baseline")], "excluded": []},
                          "collect.static.verify": make_failed(CallStatus.QUOTA_EXHAUSTED)})
    result = source.collect(runtime, caller=caller)
    assert result.status is SourceStatus.PARTIAL and result.signals == []
    assert [item["reason"] for item in result.state[source.PENDING_KEY]] == ["overLimit"]
    failed = source.collect(static_runtime(run="R-2"), caller=fake_caller({
        "collect.static.baseline": make_failed(CallStatus.TIMEOUT)}))
    assert failed.status is SourceStatus.PARTIAL and source.STATE_KEY not in failed.state
    assert "基线审查第 1/1 批" in failed.reason


def test_a_full_run_is_requested_by_level(static_runtime, fake_caller, repos):
    runtime = static_runtime()
    save(runtime, source.collect(runtime, caller=fake_caller({"collect.static.baseline": {"claims": [],
                                                                                          "excluded": []}})))
    result = source.collect(runtime, level=Level.FULL, caller=fake_caller({}))
    assert result.status is SourceStatus.DONE and result.coverage == ["README.md", "src/orders.py",
                                                                     "tests/test_orders.py"]


def test_changes_to_the_worktree_during_the_review_void_the_whole_run(static_runtime, fake_caller):
    runtime = static_runtime()
    worktree = worktree_of(runtime)

    def tamper(params):
        os.chmod(worktree, 0o755)  # 审查期间目录的写权限已被去掉，越界的 agent 自己改回来再写
        (worktree / "stray.txt").write_text("x\n", encoding="utf-8")
        return {"claims": [], "excluded": []}

    with pytest.raises(SourceUnavailable, match="整次巡检作废"):
        source.collect(runtime, caller=fake_caller({"collect.static.baseline": tamper}))
    assert not list(runtime.workspace.worktrees_dir.glob("readonly-*.json"))


def test_a_task_violation_voids_only_that_call(static_runtime, fake_caller, make_failed):
    runtime = static_runtime()
    caller = fake_caller({"collect.static.baseline": make_failed(CallStatus.BOUNDARY, ("command：rm -rf /",))})
    result = source.collect(runtime, caller=caller)
    assert result.status is SourceStatus.PARTIAL and result.signals == []
    # 审查没完成：起点不前进，下次重审
    assert source.STATE_KEY not in result.state and "基线审查第 1/1 批" in result.reason


def test_a_worktree_not_at_the_target_commit_fails(static_runtime, fake_caller, monkeypatch):
    runtime = static_runtime()
    synced = SimpleNamespace(worktree=worktree_of(runtime), previous=None, commit="0" * 40)
    monkeypatch.setattr(worktrees, "sync_readonly", lambda git, path, *, marker: synced)
    with pytest.raises(SourceUnavailable, match="先同步只读 worktree"):
        source.collect(runtime, caller=fake_caller({}))


@pytest.mark.parametrize("library_fails", [False, True])
def test_rule_library_hits_become_signals_without_review(static_runtime, fake_caller, monkeypatch, library_fails):
    runtime = static_runtime()
    rules = runtime.workspace.root / source.RULES_DIR
    rules.mkdir(parents=True)
    (rules / "PAT-0003.yaml").write_text("rules: []\n", encoding="utf-8")
    hit = ToolFinding("semgrep", "lint", "PAT-0003-no-limit", "src/orders.py", 3, 5, "分页没有上限", "high")
    asked = []

    def fake_semgrep(runner, configs, found, worktree, raw, environ, timeout_s, command):
        asked.append(list(configs))
        if not configs or not configs[0].endswith("PAT-0003.yaml"):
            return semgrep.SemgrepRun(semgrep.SKIPPED)
        if library_fails:
            return semgrep.SemgrepRun(semgrep.FAILED, notes=("Semgrep 失败：退出码 2",))
        return semgrep.SemgrepRun(semgrep.OK, (hit,))

    monkeypatch.setattr(semgrep, "run", fake_semgrep)
    caller = fake_caller({"collect.static.baseline": {"claims": [], "excluded": []}})
    result = source.collect(runtime, caller=caller)
    assert asked[1] == [str(rules / "PAT-0003.yaml")]
    if library_fails:
        assert result.status is SourceStatus.PARTIAL and "规则库" in result.reason and result.signals == []
        return
    assert result.status is SourceStatus.DONE and caller.points() == ["collect.static.baseline"]
    assert [(item.location, item.evidence["rule"], item.verified) for item in result.signals] == [
        ("src/orders.py:3", "rule:PAT-0003-no-limit", False)]
    assert result.metrics.produced["ruleHits"] == 1
