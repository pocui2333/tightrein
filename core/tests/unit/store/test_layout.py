from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from tightrein.domain.enums import ExtensionPoint, KnowledgeType, Probe, Stage, VerifyPhase
from tightrein.store.files.layout import TOOL_ROOT, ToolLayout, UserLayout, WorkspaceLayout, compact_time, segment

ROOT = Path("/w/sample")
RUN = "R-20260929-021503-collect-api-fuzz"
AT = datetime(2026, 9, 29, 2, 15, 3, tzinfo=timezone.utc)


def rel(path):
    return path.relative_to(ROOT).as_posix()


def test_workspace_files():
    layout = WorkspaceLayout(ROOT)
    assert layout.project == "sample"
    assert [rel(path) for path in (
        layout.project_config(), layout.normalize_rules(), layout.suppressions(), layout.extensions_dir(),
        layout.extension_fixtures_dir(),
        layout.knowledge_file(KnowledgeType.DEFECT_PATTERN, "DP-0012", "owner-check"),
        layout.knowledge_index(KnowledgeType.FIX_LESSON), layout.knowledge_index(),
        layout.knowledge_index_page(KnowledgeType.DEFECT_PATTERN, "DP-0001", "DP-0200"),
        layout.e2e_role_dir("Company"),
        layout.regression_checklist("0007"), layout.eval_manifest(), layout.eval_case_dir(Stage.TRIAGE, "E-0004"),
        layout.retrieval_cases(), layout.rules_dir(),
        layout.issue_file("0007", "order-owner-check"),
        layout.readonly_worktree(), layout.fix_worktree("0007"),
    )] == [
        "project.yaml", "normalize.yaml", "suppressions.yaml", "extensions", "extensions/tests/fixtures",
        "knowledge/defect-pattern/DP-0012-owner-check.md", "knowledge/fix-lesson/INDEX.md", "knowledge/INDEX.md",
        "knowledge/defect-pattern/INDEX-DP-0001-DP-0200.md",
        "e2e/Company",
        "regressions/0007/check.yaml", "evals/manifest.json", "evals/triage/E-0004",
        "evals/retrieval/cases.jsonl", "rules",
        "issues/0007-order-owner-check.md",
        "worktrees/readonly", "worktrees/fix-0007",
    ]


def test_data_files():
    layout = WorkspaceLayout(ROOT)
    assert [rel(path) for path in (
        layout.database(), layout.aggregate_lock(), layout.run_lock(), layout.suppressions_lock(),
        layout.readonly_guard("readonly"), layout.openapi("abc1234"), layout.authz_model("abc1234"),
        layout.extension_output("abc1234", ExtensionPoint.PAGE_ROUTES),
        layout.extension_meta("abc1234", ExtensionPoint.PAGE_ROUTES),
        layout.events_log(date(2026, 9, 29)), layout.launchd_out_log(), layout.launchd_err_log(),
        layout.finding("P-0042"), layout.fix_report("0007"), layout.verify_dir("0007", date(2026, 9, 30),
                                                                              VerifyPhase.LOCAL),
        layout.eval_output_dir("EV-20260929-031500"), layout.run_report("R-20260929-021503-loop"),
        layout.weekly_report(date(2026, 10, 5)), layout.database_backup(AT), layout.suppressions_backup(AT),
    )] == [
        "data/tightrein.db", "data/aggregate.lock", "data/run.lock", "data/suppressions.lock",
        "data/guards/readonly-readonly.json", "data/specs/abc1234/openapi.json", "data/specs/abc1234/authz-model.json",
        "data/specs/abc1234/page-routes.json", "data/specs/abc1234/page-routes.meta.json",
        "data/logs/events-2026-09-29.jsonl", "data/logs/launchd.out.log", "data/logs/launchd.err.log",
        "data/findings/P-0042.md", "data/fixes/0007/report.md", "data/verify/0007/2026-09-30-local",
        "data/evals/EV-20260929-031500", "data/reports/run-R-20260929-021503-loop.md",
        "data/reports/weekly-2026-10-05.md", "data/archive/tightrein-20260929-021503.db",
        "data/archive/suppressions-20260929-021503.yaml",
    ]


def test_run_files():
    layout = WorkspaceLayout(ROOT)
    run = f"data/runs/{RUN}"
    assert [rel(path) for path in (
        layout.handoff(RUN, "collect-" + RUN), layout.handoff_history(RUN, "triage-P-0042", 2),
        layout.transcript(RUN, "claim-verifier", "P-0042"), layout.signals_file(RUN),
        layout.probe_raw_dir(RUN, Probe.API_FUZZ), layout.runner_raw_dir(RUN, "judge", "0007"),
        layout.guard_report(RUN, "fix-executor", "0007"), layout.vcs_raw_dir(RUN, "OP-0015"),
        layout.spec_export_log(RUN), layout.extension_raw_dir(RUN),
        layout.extension_stderr(RUN, ExtensionPoint.LOG_PARSE),
        layout.extension_stderr(RUN, ExtensionPoint.LOG_PARSE, 2), layout.extension_trial_openapi(RUN),
    )] == [
        f"{run}/handoff/collect-{RUN}.json", f"{run}/handoff/triage-P-0042.2.json",
        f"{run}/transcripts/claim-verifier-P-0042.jsonl", f"{run}/signals.ndjson", f"{run}/raw/api-fuzz",
        f"{run}/raw/runner/judge-0007", f"{run}/raw/guards/fix-executor-0007.json", f"{run}/raw/vcs/OP-0015",
        f"{run}/raw/api-fuzz/spec-export.log", f"{run}/raw/extensions",
        f"{run}/raw/extensions/log-parse.stderr.log", f"{run}/raw/extensions/log-parse-2.stderr.log",
        f"{run}/raw/extensions/openapi.json",
    ]
    with pytest.raises(ValueError):
        layout.handoff_history(RUN, "triage-P-0042", 0)
    with pytest.raises(ValueError):
        layout.extension_stderr(RUN, ExtensionPoint.LOG_PARSE, 0)


def test_output_mode_redirects_run_files_and_events(tmp_path):
    layout = WorkspaceLayout(ROOT, output_dir=tmp_path)
    assert layout.handoff(RUN, "triage-P-0042") == tmp_path / "handoff" / "triage-P-0042.json"
    assert layout.transcript(RUN, "judge", "0007") == tmp_path / "transcripts" / "judge-0007.jsonl"
    assert layout.events_log(date(2026, 9, 29)) == tmp_path / "events.jsonl"
    assert layout.database() == ROOT / "data" / "tightrein.db"
    assert layout.extension_exchange(ExtensionPoint.LOCAL_RUN, 1, "request") == (
        tmp_path / "extensions" / "local-run.request.json")
    assert layout.extension_exchange(ExtensionPoint.LOCAL_RUN, 2, "response") == (
        tmp_path / "extensions" / "local-run-2.response.json")
    with pytest.raises(ValueError):
        layout.extension_exchange(ExtensionPoint.LOCAL_RUN, 1, "stderr")


def test_exchange_copies_exist_only_in_output_mode():
    assert WorkspaceLayout(ROOT).extension_exchange(ExtensionPoint.LOCAL_RUN, 1, "request") is None


def test_numbered_suppression_backups():
    layout = WorkspaceLayout(ROOT)
    assert rel(layout.suppressions_backup(AT, 2)) == "data/archive/suppressions-20260929-021503-2.yaml"


def test_relative_paths_for_the_database():
    layout = WorkspaceLayout(ROOT)
    assert layout.relative(layout.issue_file("0007", "x")) == "issues/0007-x.md"


@pytest.mark.parametrize("value", ["", ".", "..", "a/b", "a\\b", "../etc"])
def test_segments_cannot_escape_their_directory(value):
    with pytest.raises(ValueError):
        segment(value)
    with pytest.raises(ValueError):
        WorkspaceLayout(ROOT).issue_file("0007", value)


def test_tool_and_user_paths():
    assert (TOOL_ROOT / "core" / "tightrein" / "store" / "files" / "layout.py").is_file()
    tool = ToolLayout(Path("/repo"))
    assert tool.skill("triage") == Path("/repo/skills/triage/SKILL.md")
    assert tool.workspace("sample").root == Path("/repo/workspaces/sample")
    assert tool.third_party_lock() == Path("/repo/third_party/skills.lock.yaml")
    assert tool.third_party_installed() == Path("/repo/third_party/installed.json")
    assert tool.third_party_cache("superpowers", "abc1234") == Path("/repo/local/third_party-cache/superpowers/abc1234")
    assert [str(path) for path in (
        tool.stacks_dir(), tool.stack_dir("webstack"), tool.stack_manifest("webstack"),
        tool.method_dir("webstack", ExtensionPoint.LOG_PARSE, "console"),
        tool.method_manifest("webstack", ExtensionPoint.LOG_PARSE, "console"),
        tool.stack_fixtures_dir("webstack"),
    )] == [
        "/repo/extensions/stacks", "/repo/extensions/stacks/webstack",
        "/repo/extensions/stacks/webstack/stack.yaml",
        "/repo/extensions/stacks/webstack/log-parse/console",
        "/repo/extensions/stacks/webstack/log-parse/console/method.yaml",
        "/repo/extensions/stacks/webstack/tests/fixtures",
    ]
    with pytest.raises(ValueError):
        tool.stack_dir("../stacks")
    with pytest.raises(ValueError):
        tool.method_dir("webstack", ExtensionPoint.LOG_PARSE, "../console")
    user = UserLayout(Path("/Users/me"))
    assert [str(path) for path in (
        user.config(), user.extension_cache("webstack"),
        user.launch_agent("sample"),
    )] == [
        "/Users/me/.config/tightrein/config.yaml",
        "/Users/me/.cache/tightrein/extensions/webstack",
        "/Users/me/Library/LaunchAgents/local.tightrein.sample.plist",
    ]


def test_compact_time_is_utc():
    assert compact_time(datetime(2026, 9, 29, 11, 15, 3, tzinfo=timezone.utc)) == "20260929-111503"


def test_evaluation_files():
    layout = WorkspaceLayout(ROOT)
    evaluation = "EV-20261005-030000"
    assert [rel(path) for path in (
        layout.eval_plan(evaluation), layout.eval_scores(evaluation), layout.eval_report_json(evaluation),
        layout.eval_report_md(evaluation), layout.eval_version_dir(evaluation, "IP-0003"),
        layout.eval_project_snapshot(evaluation, "abc1234"), layout.eval_run_output(evaluation, "baseline", "E-0001", 2),
    )] == [
        f"data/evals/{evaluation}/plan.json", f"data/evals/{evaluation}/scores.jsonl",
        f"data/evals/{evaluation}/report.json", f"data/evals/{evaluation}/report.md",
        f"data/evals/{evaluation}/versions/IP-0003", f"data/evals/{evaluation}/snapshots/abc1234",
        f"data/evals/{evaluation}/outputs/baseline/E-0001/2",
    ]
    with pytest.raises(ValueError):
        layout.eval_run_output(evaluation, "baseline", "E-0001", 0)
    with pytest.raises(ValueError):
        layout.eval_version_dir(evaluation, "a/b")
