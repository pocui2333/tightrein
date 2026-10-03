"""单元测试的 slow 标记：依赖真实 git 仓库、真实子进程或单个超过约 0.2 秒的测试。

日常运行 `pytest tests/unit -m "not slow"` 跳过它们；任务完成与计划完成时运行不带 `-m` 的完整测试。
用到 `repos` 夹具(真实 git 仓库)的测试一律标记；其余已有的测试登记在 SLOW_TESTS 中，键为
「相对 tests/unit 的文件路径::测试函数名」(参数化的测试不带参数部分)。新写的测试直接用 `@pytest.mark.slow`。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

SLOW_FIXTURES = frozenset({"repos"})

SLOW_TESTS = frozenset({
    "extensions/test_ext_client.py::test_a_cache_miss_needs_the_worktree_at_the_commit",
    "extensions/test_ext_client.py::test_a_cached_spec_needs_the_document_file",
    "extensions/test_ext_client.py::test_a_workspace_spec_file_needs_no_worktree",
    "extensions/test_ext_client.py::test_failures_are_not_cached",
    "extensions/test_ext_client.py::test_local_run_plans_must_not_carry_credentials",
    "extensions/test_ext_client.py::test_log_parse_passes_chunks_and_state",
    "extensions/test_ext_client.py::test_log_source_takes_its_limits_from_the_project_config",
    "extensions/test_ext_client.py::test_spec_export_output_is_checked_beyond_the_schema",
    "extensions/test_ext_client.py::test_spec_export_writes_the_document_and_is_cached_by_commit",
    "extensions/test_ext_client.py::test_static_tools_creates_the_raw_directory",
    "extensions/test_ext_commands.py::test_fixture_schema_errors_and_missing_implementations_fail",
    "extensions/test_ext_commands.py::test_fixtures_are_compared_with_the_expected_response",
    "extensions/test_ext_commands.py::test_run_point_applies_the_checks_beyond_the_schema",
    "extensions/test_ext_commands.py::test_run_point_calls_once_without_touching_the_cache",
    "extensions/test_ext_invoke.py::test_a_crash_keeps_the_last_fifty_lines_of_stderr",
    "extensions/test_ext_invoke.py::test_a_successful_call_gets_the_request_environment_and_working_directory",
    "extensions/test_ext_invoke.py::test_each_process_writes_a_run_script_span",
    "extensions/test_ext_invoke.py::test_error_responses_are_taken_even_with_a_nonzero_exit",
    "extensions/test_ext_invoke.py::test_extend_gives_the_lower_output_as_base_and_merges_notes",
    "extensions/test_ext_invoke.py::test_extend_stops_when_the_lower_layer_fails",
    "extensions/test_ext_invoke.py::test_not_applicable_falls_back_to_the_default",
    "extensions/test_ext_invoke.py::test_output_mode_keeps_request_and_response_copies",
    "extensions/test_ext_invoke.py::test_output_over_the_limit_is_a_protocol_error",
    "extensions/test_ext_invoke.py::test_protocol_errors",
    "extensions/test_ext_invoke.py::test_schema_violations_list_every_path",
    "extensions/test_ext_invoke.py::test_stack_variables_are_added_and_the_cache_is_per_stack",
    "extensions/test_ext_invoke.py::test_stderr_is_redacted_and_numbered_per_call",
    "extensions/test_ext_invoke.py::test_timeout_terminates_the_whole_process_group",
    "extensions/test_ext_invoke.py::test_unstartable_commands_are_reported_as_crashed",
    "guards/test_readonly.py::test_recover_restores_locks_of_dead_processes",
    "observability/test_redact.py::test_long_text_is_redacted_in_linear_time",
    "probes/test_probe_procs_routes.py::test_launcher_captures_output_and_writes_a_redacted_log",
    "probes/test_probe_procs_routes.py::test_launcher_timeout_and_missing_program",
    "probes/test_probe_session.py::test_urllib_transport_reports_connection_errors",
    "probes/test_probe_session.py::test_urllib_transport_returns_status_and_body_for_errors_too",
    "retrieval/test_kb_mcp.py::test_stdio_mode_writes_only_protocol_messages",
    "retrieval/test_kb_stale_index.py::test_large_types_are_paged_and_the_root_only_links",
    "runner/test_runner_process.py::test_interactive_runs_return_the_exit_code",
    "runner/test_runner_process.py::test_lines_are_read_in_order",
    "runner/test_runner_process.py::test_missing_programs_raise",
    "runner/test_runner_process.py::test_processes_that_ignore_signals_are_killed",
    "runner/test_runner_process.py::test_stdin_is_passed_to_the_process",
    "runner/test_runner_process.py::test_the_line_handler_can_stop_the_process",
    "runner/test_runner_process.py::test_the_whole_process_group_is_terminated",
    "runner/test_runner_process.py::test_timeout_interrupts_the_process",
    "store/test_locks.py::test_file_lock_waits_in_line",
    "store/test_locks.py::test_process_alive",
    "store/test_sequences.py::test_concurrent_allocations_never_repeat",
    "vcs/test_vcs_process.py::test_the_subprocess_executor_runs_real_programs",
})


def is_slow(key: str, fixtures: Iterable[str]) -> bool:
    return key in SLOW_TESTS or not SLOW_FIXTURES.isdisjoint(fixtures)


def apply(items: list[pytest.Item], root: Path) -> None:
    """给 root 下命中的测试加 slow 标记。"""
    for item in items:
        if not item.path.is_relative_to(root):
            continue
        key = f"{item.path.relative_to(root).as_posix()}::{getattr(item, 'originalname', item.name)}"
        if is_slow(key, getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.slow)
