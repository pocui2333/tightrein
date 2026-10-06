import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from probe_world import NOW, CountingRandom, make_redactor, make_target

from tightrein.domain.enums import ExtensionLayer, ExtensionPoint, ProbeLevel, RunnerStatus, RunStatus, Source
from tightrein.extensions.result import PointResult
from tightrein.sources.base import ProbeOptions
from tightrein.sources.common.procs import ToolRun
from tightrein.sources.static.probe import StaticDependencies, StaticProbe
from tightrein.sources.static.reviewer import Claim, ReviewResult, Verification
from tightrein.store.repos.pending_claims import PendingClaim
from tightrein.vcs.git_read import GitReader
from tightrein.vcs.process import VcsProcess

VERIFIED_AT = datetime(2026, 10, 5, 3, 20, tzinfo=timezone.utc)
VULNERABILITY = {"tool": "deps-audit", "kind": "vulnerability", "rule": "GHSA-5crp-9r3c-p9vr",
                 "file": "src/App.csproj", "line": None, "column": None, "message": "Newtonsoft.Json 13.0.1 存在已知漏洞",
                 "severity": "high", "package": {"name": "Newtonsoft.Json", "version": "13.0.1", "advisoryUrl": None}}


def claim(line, rule, statement, file="src/OrderService.cs", layer="incremental"):
    return Claim(file, line, rule, layer, statement, "pageSize 为 0 时")


def verdict(value, symbol="OrderService.Page", file="src/OrderService.cs", line=3):
    return {"verdict": value, "facts": [{"location": f"{file}:{line}", "observation": "没有校验 pageSize"}],
            "trigger": "传入 pageSize=0", "counterEvidence": [],
            "impact": {"kind": "non-core-error", "roles": ["Company"], "data": "订单",
                       "callSites": ["src/Controllers/OrderController.cs:20"], "consequence": "返回 500"},
            "sourceOfPhenomenon": None, "rootCauses": [{"file": file, "line": line, "symbol": symbol}],
            "fixedOnMain": None, "tradeoffHit": None, "missingInfo": [], "incidental": []}


class FakeReviewer:
    def __init__(self, claims, verdicts, *, review_status=RunnerStatus.OK, verify_status=None, patterns=(),
                 on_review=None, violations=()):
        self.claims = claims
        self.verdicts = verdicts
        self.review_status = review_status
        self.verify_status = verify_status or {}
        self.patterns = patterns
        self.on_review = on_review
        self.reviewed = []
        self.verified = []
        self.scanned = []
        self.batches = []
        self.baseline_results = {}
        self.violations = violations

    def review(self, scope, tool_findings):
        self.reviewed.append((scope, tool_findings))
        if self.on_review is not None:
            self.on_review()
        return ReviewResult(self.review_status, tuple(self.claims), ({"file": "a", "line": 1, "statement": "s",
                                                                      "tradeoffId": "TO-0001"},),
                            "transcripts/static-review-R.jsonl", "输出不合 schema")

    def review_baseline(self, batch, tool_findings):
        self.batches.append((batch, tool_findings))
        default = ReviewResult(RunnerStatus.OK, tuple(self.claims) if batch.number == 1 else (),
                               transcript=f"transcripts/baseline-review-{batch.number}.jsonl", duration_ms=30000,
                               cost_usd=0.5)
        return self.baseline_results.get(batch.number, default)

    def defect_patterns(self):
        return self.patterns

    def scan_variants(self, pattern_id, scope):
        self.scanned.append(pattern_id)
        return ReviewResult(RunnerStatus.OK, (claim(4, pattern_id, "同类实例", layer="full"),))

    def verify(self, item):
        self.verified.append(item)
        status = self.verify_status.get(item.rule_or_pattern, RunnerStatus.OK)
        output = self.verdicts.get(item.rule_or_pattern) if status is RunnerStatus.OK else None
        return Verification(status, output, f"transcripts/claim-verifier-{item.line}.jsonl", "daily-budget",
                            VERIFIED_AT, self.violations if status is RunnerStatus.GUARD_VIOLATION else ())


class ToolsClient:
    def static_tools(self, repo, commit, scope, raw_dir):
        return PointResult(ExtensionPoint.STATIC_TOOLS, ExtensionLayer.STACK, output={
            "tools": [{"name": "deps-audit", "status": "ok", "exitCode": 0, "logFile": None, "reason": None}],
            "findings": [VULNERABILITY]})


class World:
    def __init__(self, tmp_path, repos, make_config, reviewer, **changes):
        _, self.repo = repos.origin_and_clone()
        self.base = repos.head(self.repo)
        self.head = repos.commit(self.repo, "feat: 分页", {
            "src/OrderService.cs": "class OrderService\n{\n    int Page(int size) => 10 / size;\n    int X;\n}\n"})
        self.git = GitReader(VcsProcess(environ=repos.environ))
        config = make_config(sources={"static": {"maxClaims": changes.pop("max_claims", 10)}})
        self.probe = StaticProbe(StaticDependencies(config, ToolsClient(), self.git,
                                                    lambda command: ToolRun(0, '{"results": [], "errors": []}'),
                                                    make_redactor(), {"PATH": "/bin"}, CountingRandom()))
        self.reviewer = reviewer
        self.target = make_target(tmp_path, "static", worktree=self.repo, release=changes.pop("release", self.head))

    def run(self, level=ProbeLevel.INCREMENTAL, **options):
        return self.probe.run(self.target, level, ProbeOptions(base_commit=options.pop("base", self.base),
                                                               reviewer=self.reviewer, **options))


def standard_reviewer(**changes):
    claims = [claim(3, "DP-0012", "分页参数为 0 时除零"), claim(4, "DP-0013", "字段未初始化"),
              claim(3, "DP-0014", "证据不足的主张"), claim(4, "DP-0015", "被反驳的主张"),
              claim(1, "GHSA-5crp-9r3c-p9vr", "依赖漏洞", file="src/App.csproj", layer="deterministic")]
    verdicts = {"DP-0012": verdict("confirmed"), "DP-0013": verdict("conditional", symbol=None),
                "DP-0014": verdict("insufficient"), "DP-0015": verdict("refuted"),
                "GHSA-5crp-9r3c-p9vr": verdict("confirmed", symbol=None, file="src/App.csproj", line=1)}
    return FakeReviewer(claims, verdicts, **changes)


def test_only_confirmed_and_conditional_claims_become_signals(tmp_path, repos, make_config):
    world = World(tmp_path, repos, make_config, standard_reviewer())
    outcome = world.run()
    assert outcome.status is RunStatus.OK
    by_check = {signal.check: signal for signal in outcome.signals}
    assert sorted(by_check) == ["DP-0012", "DP-0013", "GHSA-5crp-9r3c-p9vr"]
    confirmed = by_check["DP-0012"]
    assert (confirmed.source, confirmed.location, confirmed.message) == (
        Source.SYNTHETIC, "src/OrderService.cs:OrderService.Page", "分页参数为 0 时除零")
    assert confirmed.release == world.head and confirmed.occurred_at == VERIFIED_AT and confirmed.actor == {}
    context = confirmed.context
    assert (context["line"], context["layer"], context["verdict"]) == (3, "incremental",
                                                                       {"value": "confirmed", "condition": None})
    assert context["evidence"] == [{"location": "src/OrderService.cs:3", "observation": "没有校验 pageSize"}]
    assert context["callChain"] == ["src/Controllers/OrderController.cs:20"]
    assert context["introducedBy"]["commit"] == world.head and context["introducedBy"]["author"] == "Cui Ty"
    assert context["transcripts"] == ["transcripts/static-review-R.jsonl", "transcripts/claim-verifier-3.jsonl"]
    assert by_check["DP-0013"].location == "src/OrderService.cs"
    assert by_check["DP-0013"].context["verdict"] == {"value": "conditional", "condition": "传入 pageSize=0"}
    vulnerability = by_check["GHSA-5crp-9r3c-p9vr"]
    assert vulnerability.location == "src/App.csproj:Newtonsoft.Json"
    assert vulnerability.context["toolFinding"]["package"]["name"] == "Newtonsoft.Json"
    assert outcome.stats["insufficientClaims"] == 1 and outcome.stats["refutedClaims"] == 1
    assert outcome.stats["excludedClaims"] == 1 and outcome.stats["toolFindings"] == 1
    assert outcome.coverage.files == ("src/OrderService.cs",)
    scope, findings = world.reviewer.reviewed[0]
    assert scope.head == world.head and findings[0].rule == "GHSA-5crp-9r3c-p9vr"


def test_claims_over_the_limit_go_to_the_pending_list(tmp_path, repos, make_config):
    world = World(tmp_path, repos, make_config, standard_reviewer(), max_claims=2)
    outcome = world.run()
    assert len(world.reviewer.verified) == 2 and outcome.stats["pendingOverLimit"] == 3
    assert {item.reason for item in outcome.pending_claims} == {"over-limit"}
    assert len({item.id for item in outcome.pending_claims}) == 3
    assert "3 条疑点超出本次取证上限，进入待处理清单，后续运行继续取证" in outcome.notes


def test_low_claims_wait_and_queued_claims_are_verified_first(tmp_path, repos, make_config):
    low = replace(claim(5, "DP-0016", "低级疑点"), severity="low")
    reviewer = standard_reviewer()
    reviewer.claims = [low, *reviewer.claims]
    queued = PendingClaim("PC-0000000009", {"file": "src/OrderService.cs", "line": 4, "ruleOrPattern": "DP-0013",
                                            "layer": "incremental", "statement": "遗留", "trigger": "t",
                                            "severity": "high"}, "high", "over-limit", "R-0", NOW)
    world = World(tmp_path, repos, make_config, reviewer, max_claims=2)
    outcome = world.run(queued=(queued,))
    assert [item.rule_or_pattern for item in reviewer.verified][0] == "DP-0013"
    assert "DP-0016" not in [item.rule_or_pattern for item in reviewer.verified]
    by_reason = {}
    for item in outcome.pending_claims:
        by_reason.setdefault((item.reason, item.state), []).append(item.claim["ruleOrPattern"])
    assert by_reason[("low", "pending")] == ["DP-0016"]
    assert by_reason[("over-limit", "verified")] == ["DP-0013"]
    assert "1 条低级疑点进入待处理清单，选中时取证(--select pending:<编号>|low)" in outcome.notes


def test_selected_pending_claims_are_verified_without_a_review(tmp_path, repos, make_config):
    reviewer = standard_reviewer()
    selected = PendingClaim("PC-0000000001", {"file": "src/OrderService.cs", "line": 3, "ruleOrPattern": "DP-0012",
                                              "layer": "incremental", "statement": "分页", "trigger": "t",
                                              "severity": "low"}, "low", "low", "R-0", NOW)
    outcome = World(tmp_path, repos, make_config, reviewer).run(pending=("low",), queued=(selected,))
    assert reviewer.reviewed == [] and [signal.check for signal in outcome.signals] == ["DP-0012"]
    assert [(item.id, item.state) for item in outcome.pending_claims] == [("PC-0000000001", "verified")]
    assert outcome.coverage.files == ()


def test_budget_exhaustion_stops_verification(tmp_path, repos, make_config):
    reviewer = standard_reviewer(verify_status={"DP-0013": RunnerStatus.LIMIT_REACHED})
    outcome = World(tmp_path, repos, make_config, reviewer).run()
    assert outcome.status is RunStatus.PARTIAL and outcome.stats["pendingOverLimit"] == 4
    assert [signal.check for signal in outcome.signals] == ["DP-0012"]
    assert "取证达到上限(daily-budget)，其余疑点进入待处理清单" in outcome.notes


@pytest.mark.parametrize("violations", [(), ("readonly-modified",), ("hidden-path-read", "forbidden-path-modified"),
                                        ("git-commit-created",)])
def test_environment_violations_fail_the_whole_run(tmp_path, repos, make_config, violations):
    reviewer = standard_reviewer(verify_status={"DP-0014": RunnerStatus.GUARD_VIOLATION}, violations=violations)
    outcome = World(tmp_path, repos, make_config, reviewer).run()
    assert outcome.status is RunStatus.FAILED and outcome.signals == ()


def test_a_task_violation_voids_only_that_task(tmp_path, repos, make_config):
    reviewer = standard_reviewer(verify_status={"DP-0012": RunnerStatus.GUARD_VIOLATION},
                                 violations=("hidden-path-read",))
    outcome = World(tmp_path, repos, make_config, reviewer).run()
    assert outcome.status is RunStatus.PARTIAL
    assert sorted(signal.check for signal in outcome.signals) == ["DP-0013", "GHSA-5crp-9r3c-p9vr"]
    assert "src/OrderService.cs:3 的取证 的边界检查违规(hidden-path-read)，该任务的产出作废" in outcome.notes


def test_a_baseline_batch_violation_voids_only_that_batch(tmp_path, repos, make_config):
    reviewer = standard_reviewer()
    reviewer.baseline_results[1] = ReviewResult(RunnerStatus.GUARD_VIOLATION, tuple(reviewer.claims),
                                                violations=("hidden-path-read",))
    world = baseline_world(tmp_path, repos, make_config, reviewer)
    outcome = world.run(ProbeLevel.BASELINE)
    first = reviewer.batches[0][0]
    assert len(reviewer.batches) == outcome.stats["baselineBatches"] > 1 and reviewer.verified == []
    assert outcome.status is RunStatus.PARTIAL and outcome.signals == ()
    assert f"基线审查{first.describe()} 的边界检查违规(hidden-path-read)，该任务的产出作废" in outcome.notes
    reviewer.baseline_results[1] = ReviewResult(RunnerStatus.GUARD_VIOLATION, violations=("readonly-modified",))
    reviewer.batches.clear()
    assert world.run(ProbeLevel.BASELINE).status is RunStatus.FAILED and len(reviewer.batches) == 1


def test_changes_to_the_worktree_fail_the_whole_run(tmp_path, repos, make_config):
    world = World(tmp_path, repos, make_config, None)
    world.reviewer = standard_reviewer(on_review=lambda: (world.repo / "src" / "Stray.cs").write_text("x"))
    changed = world.run()
    assert changed.status is RunStatus.FAILED and changed.notes[0].startswith("只读 worktree 在审查期间被改动")


def test_failed_review_makes_the_run_partial(tmp_path, repos, make_config):
    outcome = World(tmp_path, repos, make_config, standard_reviewer(review_status=RunnerStatus.SCHEMA_INVALID)).run()
    assert outcome.status is RunStatus.PARTIAL and outcome.signals == ()
    assert "增量审查 未完成(schema-invalid)：输出不合 schema，其主张不产出信号" in outcome.notes
    assert "没有配置 sources.static.semgrep.configs，不运行 Semgrep" in outcome.notes


def test_full_level_scans_every_defect_pattern(tmp_path, repos, make_config):
    reviewer = standard_reviewer(patterns=("DP-0001", "DP-0002"))
    reviewer.verdicts.update({"DP-0001": verdict("confirmed"), "DP-0002": verdict("refuted")})
    outcome = World(tmp_path, repos, make_config, reviewer).run(ProbeLevel.FULL)
    assert reviewer.scanned == ["DP-0001", "DP-0002"] and "DP-0001" in {signal.check for signal in outcome.signals}
    assert "src/OrderService.cs" in outcome.coverage.files and len(outcome.coverage.files) == 4


def test_skips_and_preconditions(tmp_path, repos, make_config):
    world = World(tmp_path, repos, make_config, standard_reviewer())
    same = world.run(base=world.head)
    assert (same.status, same.skipped_reason) == (RunStatus.SKIPPED, "上次巡检以来没有新提交")
    moved = world.probe.run(replace(world.target, release="0" * 40), None,
                            ProbeOptions(base_commit=world.base, reviewer=world.reviewer))
    assert moved.status is RunStatus.FAILED and "tightrein project worktree sync" in moved.notes[0]
    missing = world.probe.run(world.target, None, ProbeOptions())
    assert missing.status is RunStatus.FAILED and "Reviewer" in missing.notes[0]
    assert world.reviewer.reviewed == [] and NOW.tzinfo is not None


def baseline_world(tmp_path, repos, make_config, reviewer, **baseline):
    world = World(tmp_path, repos, make_config, reviewer)
    config = make_config(sources={"static": {"maxClaims": 10, "baseline": {"batchFiles": 1, **baseline}}})
    world.probe = StaticProbe(replace(world.probe.deps, config=config))
    return world


def test_first_full_run_is_a_baseline_review_in_batches(tmp_path, repos, make_config):
    reviewer = standard_reviewer(patterns=("DP-0001",))
    reviewer.verdicts["DP-0001"] = verdict("refuted")
    world = baseline_world(tmp_path, repos, make_config, reviewer)
    outcome = world.run(ProbeLevel.FULL, base=None)
    assert reviewer.reviewed == [] and reviewer.scanned == ["DP-0001"]
    batches = [batch for batch, _ in reviewer.batches]
    assert "src/OrderService.cs" in {path for batch in batches for path in batch.paths}
    assert all(len(batch.files) == 1 for batch in batches) and batches[0].total == len(batches)
    first_findings = reviewer.batches[0][1]
    assert [finding.rule for finding in first_findings] == ["GHSA-5crp-9r3c-p9vr"]
    assert outcome.status is RunStatus.OK and "DP-0012" in {signal.check for signal in outcome.signals}
    stats = outcome.stats
    assert stats["baselineBatches"] == len(batches) and stats["baselineDurationMs"] == 30000 * len(batches)
    assert stats["baselineCostUsd"] == 0.5 * len(batches)
    assert "第一次巡检，扫描范围为全部文件，按基线审查" in outcome.notes
    assert any(note.startswith(f"基线审查共 {len(batches)} 批") for note in outcome.notes)
    assert f"基线审查{batches[0].describe()}：耗时 30 秒，费用 $0.50" in outcome.notes


def test_later_full_runs_review_the_diff(tmp_path, repos, make_config):
    reviewer = standard_reviewer()
    baseline_world(tmp_path, repos, make_config, reviewer).run(ProbeLevel.FULL)
    assert reviewer.batches == [] and len(reviewer.reviewed) == 1


def test_baseline_claims_over_the_limit_keep_the_most_severe(tmp_path, repos, make_config):
    claims = [replace(claim(3, "DP-0012", "低"), severity="low"), replace(claim(4, "DP-0013", "高"), severity="high"),
              claim(5, "DP-0014", "未标注"), replace(claim(6, "DP-0015", "中"), severity="medium")]
    reviewer = FakeReviewer(claims, {item.rule_or_pattern: verdict("confirmed") for item in claims})
    world = baseline_world(tmp_path, repos, make_config, reviewer, maxClaims=2)
    outcome = world.run(ProbeLevel.BASELINE)
    assert [item.rule_or_pattern for item in reviewer.verified] == ["DP-0013", "DP-0015"]
    pending = {item.claim["ruleOrPattern"]: item.reason for item in outcome.pending_claims}
    assert pending == {"DP-0012": "low", "DP-0014": "over-limit"}


def test_an_exhausted_daily_budget_stops_the_remaining_batches(tmp_path, repos, make_config):
    reviewer = standard_reviewer()
    reviewer.baseline_results[1] = ReviewResult(RunnerStatus.LIMIT_REACHED, detail="daily-budget", exhausted=True)
    world = baseline_world(tmp_path, repos, make_config, reviewer)
    outcome = world.run(ProbeLevel.BASELINE)
    total = outcome.stats["baselineBatches"]
    assert len(reviewer.batches) == 1 and total > 1 and outcome.status is RunStatus.PARTIAL
    assert f"当天预算已用尽，其余 {total - 1} 批未审查；改天或调高 stages.collect.budgetPerDay 后再运行 --level baseline" \
        in outcome.notes
    assert "baselineCostUsd" not in outcome.stats


def test_rule_library_hits_become_signals_without_review(tmp_path, repos, make_config):
    library = tmp_path / "rules"
    library.mkdir()
    (library / "0007-divide-by-size.yaml").write_text("rules: []\n", encoding="utf-8")
    hit = {"results": [{"check_id": "divide-by-size", "path": "src/OrderService.cs", "start": {"line": 3},
                        "extra": {"message": "除数来自参数", "severity": "WARNING"}}], "errors": []}
    commands = []

    def launcher(command):
        commands.append(command)
        library_run = str(library / "0007-divide-by-size.yaml") in command.argv
        return ToolRun(0, json.dumps(hit) if library_run else '{"results": [], "errors": []}')

    world = World(tmp_path, repos, make_config, standard_reviewer())
    world.probe = StaticProbe(replace(world.probe.deps, launcher=launcher, rules_dir=library))
    outcome = world.run()
    (signal,) = [item for item in outcome.signals if item.check.startswith("rule:")]
    assert (signal.check, signal.location, signal.message) == ("rule:divide-by-size", "src/OrderService.cs:3", "除数来自参数")
    assert signal.context["rule"] == {"id": "divide-by-size", "message": "除数来自参数"}
    assert outcome.stats["ruleHits"] == 1 and outcome.status is RunStatus.OK
    assert all("divide-by-size" not in str(item) for item in world.reviewer.reviewed[0][1])


def test_a_failed_rule_library_run_makes_the_run_partial(tmp_path, repos, make_config):
    library = tmp_path / "rules"
    library.mkdir()
    (library / "0007-divide-by-size.yaml").write_text("rules: []\n", encoding="utf-8")
    world = World(tmp_path, repos, make_config, standard_reviewer())
    world.probe = StaticProbe(replace(world.probe.deps, rules_dir=library,
                                      launcher=lambda command: ToolRun(2, "", "规则无法解析")))
    outcome = world.run()
    assert outcome.status is RunStatus.PARTIAL
    assert any(note.startswith("规则库：Semgrep 失败") for note in outcome.notes)
