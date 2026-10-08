from __future__ import annotations

import shlex

from tightrein.implement.check.affected import (
    Planned,
    ProjectCommand,
    affected_tests,
    argv_text,
    configured,
    index_tests,
    plan,
    remaining,
    selected,
)

FILES = ("src/orders.py", "src/users.py", "tests/test_orders.py", "web/cart.ts", "web/cart.spec.ts", "pkg/api.go",
         "pkg/api_test.go", "Svc/Orders.cs", "Tests/OrdersTests.cs")
PATTERNS = ("tests/", "*.spec.ts", "*_test.go", "*Tests.cs")
TEST = ProjectCommand("test", "pytest -q {tests}")
LINT = ProjectCommand("lint", "ruff check .")


def test_affected_tests_are_inferred_from_names_and_test_patterns() -> None:
    index = index_tests(FILES, PATTERNS)
    assert affected_tests(["src/orders.py"], index, PATTERNS) == ["tests/test_orders.py"]
    assert affected_tests(["web/cart.ts", "pkg/api.go", "Svc/Orders.cs"], index, PATTERNS) == [
        "web/cart.spec.ts", "pkg/api_test.go", "Tests/OrdersTests.cs"]
    # 改的本身是测试就跑它；没有对得上的测试时为空(整组跑)
    assert affected_tests(["tests/test_orders.py", "src/users.py"], index, PATTERNS) == ["tests/test_orders.py"]
    assert affected_tests(["README.md"], index, PATTERNS) == []


def test_the_tests_placeholder_expands_or_disappears() -> None:
    assert shlex.split(argv_text(TEST, ["tests/a b.py", "tests/c.py"])) == ["pytest", "-q", "tests/a b.py",
                                                                           "tests/c.py"]
    assert argv_text(TEST, []) == "pytest -q"
    assert argv_text(LINT, ["tests/c.py"]) == "ruff check ."
    assert configured({"test": "pytest", "build": None, "lint": ""}) == [ProjectCommand("test", "pytest")]


def test_full_runs_everything_and_correction_rounds_only_what_is_affected() -> None:
    assert plan([TEST, LINT], full=True, tests=["tests/x.py"], changed_this_round=[], previous={}) == [
        Planned(TEST), Planned(LINT)]
    narrowed = plan([TEST, LINT], full=False, tests=["tests/test_orders.py"], changed_this_round=[],
                    previous={"test": "failed", "lint": "passed"})
    assert narrowed == [Planned(TEST, ("tests/test_orders.py",))]
    # 上一轮没通过的照样重跑；这一轮又改了文件时重跑其他检查
    assert plan([TEST, LINT], full=False, tests=[], changed_this_round=["src/a.py"],
                previous={"test": "failed", "lint": "passed"}) == [Planned(TEST), Planned(LINT)]
    assert plan([TEST, LINT], full=False, tests=[], changed_this_round=[],
                previous={"test": "passed", "lint": "passed"}) == []
    # 都过了以后补跑：没跑过的与只跑了一部分测试的
    assert remaining([TEST, LINT], narrowed) == [Planned(TEST), Planned(LINT)]
    assert remaining([TEST, LINT], [Planned(LINT)]) == [Planned(TEST)]


def test_when_selects_commands_by_the_changed_paths() -> None:
    build = ProjectCommand("build", "npm run build")
    when = {"build": ["web/", "*.ts"]}
    assert selected([TEST, LINT, build], ["src/orders.py"], when) == [TEST, LINT]
    assert selected([TEST, LINT, build], ["src/orders.py", "web/cart.ts"], when) == [TEST, LINT, build]
    assert selected([TEST, build], [], {}) == [TEST, build]  # 没写 when 的总是跑
