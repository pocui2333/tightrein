import pytest

from tightrein.domain.enums import ViolationKind
from tightrein.guards import diff_rules
from tightrein.guards.diff_rules import ChangedFile
from tightrein.config import layers
from tightrein.guards.policy import GuardSettings
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.vcs.git_read import GitReader
from tightrein.vcs.process import VcsProcess

SETTINGS = GuardSettings(
    protected_paths=("Migrations/MigrationList.cs", "deploy/"), protected_patterns=("[Authorize]", "[AllowAnonymous]"),
    test_paths=("tests/", "*.test.js"), max_files=2, max_lines=5,
)


def kinds(violations):
    return [(item.kind, item.path) for item in violations]


def test_settings_come_from_the_project_config(make_config):
    settings = GuardSettings.from_config(make_config())
    assert settings.protected_paths == ("Migrations/MigrationList.cs", "deploy/")
    assert settings.test_paths == ("tests/", "*.test.js")
    assert (settings.max_files, settings.max_lines) == (3, 20)
    assert settings.skip_markers == tuple(layers.core_value("skipMarkers"))
    assert settings.credential_files == tuple(layers.core_value("credentialFiles"))
    assert settings.min_literal_length == 4
    required = {"suppressionDays": {"value": 30, "min": 7, "max": 90},
                "triage": {"deferredReopenOccurrences": {"value": 3, "min": 1, "max": 10}}}
    defaults = GuardSettings.from_config(make_config(thresholds=required, skipMarkers=["@Disabled"],
                                                     credentialFiles=["vault.txt"]))
    assert (defaults.max_files, defaults.max_lines, defaults.skip_markers) == (5, 200, ("@Disabled",))
    assert defaults.credential_files == (*layers.core_value("credentialFiles"), "vault.txt")


def test_literals():
    line = 'var order = Find("ORD-20260929", 12345, 3.75) ?? \'fallback\' + "id" + x1234;'
    assert diff_rules.literals(line, 4) == {"ORD-20260929", "12345", "3.75", "fallback"}
    assert diff_rules.literals('say("a \\" quote")', 4) == {'a \\" quote'}


def test_protected_paths_and_patterns():
    changes = [
        ChangedFile("Migrations/MigrationList.cs", ("x",)),
        ChangedFile("deploy/app.yaml", ("x",)),
        ChangedFile("src/OrderController.cs", ("    [AllowAnonymous]",), ("    [Authorize]",)),
        ChangedFile("src/Other.cs", ("var a = 1;",)),
    ]
    found = diff_rules.protected_violations(changes, SETTINGS, approved=())
    assert kinds(found) == [(ViolationKind.PROTECTED_MODIFIED, "Migrations/MigrationList.cs"),
                            (ViolationKind.PROTECTED_MODIFIED, "deploy/app.yaml"),
                            (ViolationKind.PROTECTED_MODIFIED, "src/OrderController.cs"),
                            (ViolationKind.PROTECTED_MODIFIED, "src/OrderController.cs")]
    assert [item.detail for item in found[2:]] == ["新增了 [AllowAnonymous]", "删除了 [Authorize]"]
    approved = diff_rules.protected_violations(changes, SETTINGS, approved=("Migrations/MigrationList.cs",
                                                                            "src/OrderController.cs"))
    assert kinds(approved) == [(ViolationKind.PROTECTED_MODIFIED, "deploy/app.yaml")]


def test_tests_and_skip_markers():
    changes = [
        ChangedFile("tests/OrderTests.cs", ('    [Fact(Skip = "flaky")]',)),
        ChangedFile("web/order.test.js", ("  xit('loads', () => {",)),
        ChangedFile("src/A.ts", ("// @ts-ignore", "process.exit(1);"), ("// @ts-ignore",)),
    ]
    assert kinds(diff_rules.modified_test_violations(changes, SETTINGS)) == [
        (ViolationKind.TEST_MODIFIED, "tests/OrderTests.cs"), (ViolationKind.TEST_MODIFIED, "web/order.test.js"),
    ]
    assert kinds(diff_rules.skip_violations(changes, SETTINGS)) == [
        (ViolationKind.SKIP_MARKER_ADDED, "tests/OrderTests.cs"),
        (ViolationKind.SKIP_MARKER_ADDED, "web/order.test.js"),
        (ViolationKind.SKIP_MARKER_ADDED, "src/A.ts"),
    ]


def test_suspected_hardcode(tmp_path):
    layout = WorkspaceLayout(tmp_path / "ws")
    directory = layout.regression_dir("0007")
    directory.mkdir(parents=True)
    (directory / "check.yaml").write_text("issue: '0007'\nchecks: []\n", encoding="utf-8")
    (directory / "api-1.http").write_text('GET /api/orders?no="ORD-20260929"\nexpect: 404\n', encoding="utf-8")
    reproduction = diff_rules.reproduction_literals(layout, "0007", 4)
    assert reproduction == {"ORD-20260929"}
    changes = [ChangedFile("src/OrderService.cs", ('if (no == "ORD-20260929") return null;',))]
    found = diff_rules.hardcode_violations(changes, reproduction, 4)
    assert kinds(found) == [(ViolationKind.SUSPECTED_HARDCODE, "src/OrderService.cs")]
    assert diff_rules.reproduction_literals(layout, "0008", 4) == set()


def test_size_limits():
    small = [ChangedFile("a", lines_added=2), ChangedFile("b", lines_removed=3)]
    assert diff_rules.size_violations(small, SETTINGS) == []
    tests = [ChangedFile("tests/test_a.py", lines_added=40), ChangedFile("web/a.test.js", lines_added=10)]
    assert diff_rules.size_violations([*small, *tests], SETTINGS) == []
    large = [*small, ChangedFile("c", lines_added=1)]
    assert [item.detail for item in diff_rules.size_violations(large, SETTINGS)] == [
        "改动了 3 个文件(不含测试)，超过上限 2", "增删 6 行(不含测试)，超过上限 5",
    ]


@pytest.fixture
def clone(repos):
    _, repo = repos.origin_and_clone()
    return repos, repo, GitReader(VcsProcess(environ=repos.environ))


def test_residue_and_files_outside_the_plan():
    settings = GuardSettings(residue_patterns=(r"console\.log\(", r"^\s*//\s*old:"))
    changes = [ChangedFile("src/a.js", added=("  console.log(order);", "return order;"), removed=("console.log(x);",)),
               ChangedFile("src/b.js", added=("// old: return 1;",)), ChangedFile("scratch.txt", added=("x",))]
    assert kinds(diff_rules.residue_violations(changes, settings)) == [
        (ViolationKind.RESIDUE_ADDED, "src/a.js"), (ViolationKind.RESIDUE_ADDED, "src/b.js")]
    assert diff_rules.residue_violations(changes, GuardSettings()) == []
    assert kinds(diff_rules.outside_plan_violations(changes, ["src/a.js", "src/b.js"])) == [
        (ViolationKind.OUTSIDE_PLAN, "scratch.txt")]


def test_residue_patterns_come_from_the_checks_config(make_config):
    assert GuardSettings.from_config(make_config()).residue_patterns == ()
    config = make_config(checks={"residuePatterns": ["debugger;"]})
    assert GuardSettings.from_config(config).residue_patterns == ("debugger;",)


def test_collect_changes_includes_untracked_files(clone):
    repos, repo, git = clone
    base = repos.head(repo)
    repos.write(repo, "src/OrderService.cs", "class OrderService\n{\n    [Authorize]\n    int Page = 1;\n}\n")
    repos.write(repo, "src/New.cs", "line 1\nline 2\n")
    repos.write(repo, "bin/app.dll", "ignored")
    changes = diff_rules.collect_changes(git, repo, base)
    assert [(item.path, item.added, item.lines_added) for item in changes] == [
        ("src/New.cs", ("line 1", "line 2"), 2), ("src/OrderService.cs", ("    [Authorize]",), 1),
    ]
    assert [item.path for item in diff_rules.collect_changes(git, repo, base, ["src/New.cs"])] == ["src/New.cs"]
    assert diff_rules.collect_changes(git, repo, base, []) == []
