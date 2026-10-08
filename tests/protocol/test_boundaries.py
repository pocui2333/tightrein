"""边界：路径模式(gitignore 常用子集)、两级受保护文件、改动计数不算测试与生成文件、每轮检查、命令白名单、关卡。"""

import json
from pathlib import Path

import pytest

from tightrein.protocol import boundaries
from tightrein.protocol.boundaries import Change, Violation
from tightrein.settings.load import ProjectFacts, Settings

DEFAULTS = Path(__file__).resolve().parents[2] / "settings" / "defaults.json"
PROJECT = ProjectFacts(repo=None, main_branch="main", language="zh", test_patterns=("tests/", "*.test.js"))


def make_settings(*overrides):
    return Settings.from_data(json.loads(DEFAULTS.read_text(encoding="utf-8")), *overrides, project=PROJECT)


@pytest.fixture
def settings():
    return make_settings()


@pytest.mark.parametrize("path, pattern", [
    ("web/src/order.test.js", "*.test.js"),
    ("tests/OrderTests.cs", "tests/"),
    ("src/Api/tests/Helper.cs", "tests/"),
    (".github/workflows/deploy.yml", ".github/workflows/"),
    ("Migrations/MigrationList.cs", "Migrations/MigrationList.cs"),
    ("src/Auth/PermissionMatrix.cs", "PermissionMatrix.cs"),
    ("src/Api/appsettings.Development.json", "src/*/appsettings*.json"),
    ("config/.env", ".env"),
    ("config/.env.production", ".env.*"),
    ("README.md", "/README.md"),
    ("src/Migrations/001.sql", "src/Migrations"),
])
def test_matching_paths(path, pattern):
    assert boundaries.matches_path(path, pattern)


@pytest.mark.parametrize("path, pattern", [
    ("web/src/order.js", "*.test.js"),
    ("tests", "tests/"),
    ("src/contests/A.cs", "tests/"),
    ("src/Migrations/MigrationList.cs", "Migrations/MigrationList.cs"),
    ("docs/README.md", "/README.md"),
    ("src/Auth/permissionmatrix.cs", "PermissionMatrix.cs"),
    ("src/A.cs", "/"),
])
def test_non_matching_paths(path, pattern):
    assert not boundaries.matches_path(path, pattern)


def test_two_levels_of_protected_files(settings):
    paths = ["config/.env.local", "certs/server.pem", "src/a.py", ".github/workflows/ci.yml", "db/migrations/001.sql",
             "package.json", "src/auth/login.py", "config/secrets.prod.json"]
    assert boundaries.forbidden(paths, settings) == ["certs/server.pem", "config/.env.local",
                                                     "config/secrets.prod.json"]
    assert boundaries.high_risk(paths, settings) == [".github/workflows/ci.yml", "config/secrets.prod.json",
                                                     "db/migrations/001.sql", "package.json", "src/auth/login.py"]


def test_projects_can_only_add_protected_paths(settings):
    added = make_settings({"boundaries": {"protected": {"forbidden+": ["vault/"]}}})
    assert boundaries.forbidden(["vault/key.txt", ".env"], added) == [".env", "vault/key.txt"]


def test_counts_leave_out_tests_locks_and_generated_files(settings):
    changes = [Change("src/a.py", 10, 2, "M"), Change("src/b.py", 3, 0, "?"), Change("tests/test_a.py", 50, 0, "A"),
               Change("web/a.test.js", 9, 9, "M"), Change("package-lock.json", 900, 300, "M"),
               Change("dist/app.js", 4000, 0, "A"), Change("src/__snapshots__/a.snap", 30, 0, "A")]
    assert boundaries.counted(changes, settings) == (2, 15)


def test_a_round_within_bounds_has_no_violations(settings, tmp_path):
    changes = [Change("src/a.py", 10, 2, "M"), Change("tests/test_a.py", 500, 0, "A")]
    assert boundaries.check_round(changes, worktree=tmp_path, settings=settings, project=PROJECT) == []


def test_a_round_reports_forbidden_files_and_the_cap(settings, tmp_path):
    changes = [Change(f"src/m{n}.py", 50, 0, "M") for n in range(11)] + [Change(".env", 1, 0, "M")]
    found = boundaries.check_round(changes, worktree=tmp_path, settings=settings, project=PROJECT)
    assert found == [
        Violation("forbidden", ".env", "命中禁改文件"),
        Violation("over_cap", None, "改动了 12 个文件(不含测试与生成文件)，超过上限 10"),
        Violation("over_cap", None, "增删 551 行(不含测试与生成文件)，超过上限 400"),
    ]


def test_changes_outside_the_worktree_are_violations(settings, tmp_path):
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "escape").symlink_to(tmp_path)
    changes = [Change("../outside.txt", 1, 0, "?"), Change("src/escape/x.txt", 1, 0, "?"), Change("src/a.py", 1, 0, "M")]
    found = boundaries.check_round(changes, worktree=worktree, settings=settings, project=PROJECT)
    assert [(item.kind, item.path) for item in found] == [("outside", "../outside.txt"),
                                                          ("outside", "src/escape/x.txt")]


def test_tasks_that_only_write_tests_may_touch_nothing_else(settings, tmp_path):
    changes = [Change("tests/test_a.py", 5, 0, "A"), Change("src/a.py", 1, 0, "M")]
    found = boundaries.check_round(changes, worktree=tmp_path, settings=settings, project=PROJECT, tests_only=True)
    assert [(item.kind, item.path) for item in found] == [("outside", "src/a.py")]


@pytest.mark.parametrize("command, allowed", [
    ("git grep -n Order", True),
    ("git log --oneline -5", True),
    ("grep -rn 'a|b' src", True),
    ("cat src/a.py", True),
    ("git push origin main", False),
    ("rm -rf src", False),
    ("cat a; rm -rf src", False),
    ("cat a && rm b", False),
    ("cat a | sh", False),
    ("cat a > b", False),
    ("cat $(rm b)", False),
    ("cat `rm b`", False),
    ("git -C /tmp grep x", False),
    ("cat 'unclosed", False),
    ("", False),
])
def test_the_command_allowlist(settings, command, allowed):
    assert boundaries.command_allowed(command, settings.get("boundaries.readCommands")) is allowed


def test_project_commands_extend_the_allowlist(settings):
    allowed = [*settings.get("boundaries.readCommands"), "npm test"]
    assert boundaries.command_allowed("npm test -- order", allowed)
    assert not boundaries.command_allowed("npm install left-pad", allowed)


def test_mandatory_gates_can_never_be_automatic(settings):
    assert boundaries.MANDATORY_GATES == {"forbidden_changed", "high_risk_merge", "over_cap", "needs_decision"}
    assert boundaries.gate_is_auto("merge", settings)
    assert not boundaries.gate_is_auto("merge", make_settings({"boundaries": {"gates": {"merge": "manual"}}}))
    forced = make_settings({"boundaries": {"gates": {"over_cap": "auto"}}})
    assert not boundaries.gate_is_auto("over_cap", forced)


# 读取越界


def test_hidden_reads_cover_hidden_directories_and_existing_credential_files(tmp_path):
    workdir, home = tmp_path / "work", tmp_path / "home"
    data = tmp_path / "workspace" / "data"
    for path in (workdir / "conf", home / ".ssh", data / "issues" / "0018"):
        path.mkdir(parents=True)
    (workdir / "conf" / "server.pem").write_text("x\n", encoding="utf-8")
    calls = [
        ("Read", {"file_path": "~/.ssh/id_ed25519"}),
        ("Bash", {"command": f"cat '{data}/issues/0018/handoff.json' | head"}),
        ("Read", {"file_path": "conf/server.pem"}),
        ("Grep", {"pattern": "it.key", "path": "src"}),  # 代码片段，不是存在的文件
        ("Read", {"file_path": "conf/server.pem"}),  # 同一路径只说明一次
        ("StructuredOutput", {"evidence": "见 ~/.ssh/config"}),  # 只提交结果的工具不检查
    ]
    found = boundaries.hidden_reads(calls, workdir=workdir, hidden=(home / ".ssh", data),
                                    credential_patterns=("*.pem", "*.key"), home=home)
    assert found == [
        "hidden_read：Read 访问了隐藏目录 ~/.ssh/id_ed25519",
        f"hidden_read：Bash 访问了隐藏目录 {data}/issues/0018/handoff.json",
        "hidden_read：Read 访问了凭据文件(匹配 *.pem) conf/server.pem",
    ]
    allowed = boundaries.hidden_reads(calls[1:2], workdir=workdir, hidden=(data,), credential_patterns=(),
                                      allowed=(data / "issues" / "0018",))
    assert allowed == []
