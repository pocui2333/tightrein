from __future__ import annotations

from pathlib import Path

from tightrein.implement.check.output import TRUNCATED, read_tail, trim


def test_trim_keeps_failures_and_drops_passes() -> None:
    raw = ("test_a.py::test_pass1 PASSED\n"
           "test_a.py::test_pass2 PASSED\n"
           "FAIL: test_b (tests.test_b)\n"
           "Traceback (most recent call last):\n"
           "  File \"test_b.py\", line 10, in test_b\n"
           "    assert False\n"
           "AssertionError\n"
           "=================== 1 failed, 2 passed in 0.12s ===================\n")
    trimmed = trim(raw, max_chars=8000)
    assert "AssertionError" in trimmed and "FAIL: test_b" in trimmed
    assert "test_pass1 PASSED" not in trimmed and "test_pass2 PASSED" not in trimmed
    assert "1 failed, 2 passed" in trimmed


def test_trim_keeps_failure_lines_that_mention_passed() -> None:
    # 73f1058：通过行只认 `xxx::yyy PASSED`，失败详情里含 PASSED 的行不能结束失败段落
    raw = ("tests/test_a.py::test_ok PASSED\n"
           "FAILED tests/test_a.py::test_status - AssertionError\n"
           "    assert status == 'PASSED'\n"
           "E   AssertionError: expected PASSED, got FAILED\n"
           "tests/test_a.py:12: AssertionError\n")
    trimmed = trim(raw, max_chars=8000)
    assert "E   AssertionError: expected PASSED, got FAILED" in trimmed
    assert "tests/test_a.py:12: AssertionError" in trimmed
    assert "test_ok PASSED" not in trimmed


def test_trim_keeps_unrecognized_output_and_its_tail() -> None:
    assert trim("something odd\nhappened", max_chars=8000) == "something odd\nhappened"
    trimmed = trim("x" * 50 + "END", max_chars=10)
    assert trimmed.startswith(TRUNCATED) and trimmed.endswith("xxxxxxxEND")


def test_read_tail_reads_only_the_end_and_drops_the_partial_line(tmp_path: Path) -> None:
    log = tmp_path / "test.log"
    log.write_text("first line\n" + "x" * 30 + "\nlast line\n", encoding="utf-8")
    assert read_tail(log, 15) == "last line\n"
    assert read_tail(log, 10_000).startswith("first line")
    assert read_tail(tmp_path / "missing.log", 10) == ""
