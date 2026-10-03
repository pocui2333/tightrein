import pytest

from tightrein.guards.protected import contains, matches_path, matching_markers, matching_pattern


@pytest.mark.parametrize("path, pattern", [
    ("web/src/order.test.js", "*.test.js"),
    ("tests/OrderTests.cs", "tests/"),
    ("src/Api/tests/Helper.cs", "tests/"),
    ("deploy/k8s/app.yaml", "deploy/"),
    (".github/workflows/deploy.yml", ".github/"),
    ("Migrations/MigrationList.cs", "Migrations/MigrationList.cs"),
    ("src/Auth/PermissionMatrix.cs", "PermissionMatrix.cs"),
    ("src/Api/appsettings.Development.json", "src/*/appsettings*.json"),
    ("config/.env", ".env"),
    ("README.md", "/README.md"),
    ("src/Migrations/001.sql", "src/Migrations"),
])
def test_matching_paths(path, pattern):
    assert matches_path(path, pattern)


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
    assert not matches_path(path, pattern)


def test_first_matching_pattern():
    assert matching_pattern("deploy/run.sh", ["*.test.js", "deploy/", "*.sh"]) == "deploy/"
    assert matching_pattern("src/A.cs", ["*.test.js"]) is None


def test_content_patterns_are_literal():
    assert contains("    [Authorize]", "[Authorize]")
    assert not contains("    [A]", "[Authorize]")
    assert contains("  xit('works', () => {", "xit(")
    assert not contains("  process.exit(1);", "xit(")
    assert contains("describe.skip('x')", ".skip(")
    assert contains("// @ts-ignore", "@ts-ignore")
    assert not contains("anything", "")


def test_matching_markers():
    markers = [".skip(", "@unittest.skip", "pytest.mark.skip", "#pragma warning disable"]
    assert matching_markers("@pytest.mark.skip(reason='x')", markers) == [".skip(", "pytest.mark.skip"]
    assert matching_markers("#pragma warning disable CS0618", markers) == ["#pragma warning disable"]
    assert matching_markers("var skip = 1;", markers) == []
