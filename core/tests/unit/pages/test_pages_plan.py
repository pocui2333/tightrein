import json
from pathlib import Path

from tightrein.pipeline.checks.pages import invoke, plan


def build(tmp_path, spec_dirs=()):
    return plan.build(base_url="https://staging.example.test", locale="zh-CN", retries=2,
                      accounts={"Company": "test-company", "Personal": "test-personal"},
                      workspace_e2e=tmp_path / "e2e", auth_dir=tmp_path / "auth", raw_dir=tmp_path / "raw",
                      ignore_requests=[{"method": "GET", "pathPattern": "^/api/Admin/", "status": 403}],
                      spec_dirs=spec_dirs)


def test_projects_per_role(tmp_path):
    document = build(tmp_path)
    assert [item["name"] for item in document["projects"]] == [
        "setup-Company", "patrol-Company", "setup-Personal", "patrol-Personal"]
    setup, e2e = document["projects"][:2]
    assert setup == {"name": "setup-Company", "kind": "setup", "role": "Company", "account": "test-company",
                     "storageState": str(tmp_path / "auth" / "Company.json")}
    assert e2e["testDir"] == str(tmp_path / "e2e")
    assert e2e["testMatch"] == ["Company/**/*.spec.ts", "common/**/*.spec.ts"]
    assert plan.selected_projects(document) == ["patrol-Company", "patrol-Personal"]


def test_anonymous_has_no_setup_or_login_state(tmp_path):
    document = plan.build(base_url="https://h", locale="zh-CN", retries=2, accounts={"anonymous": None},
                          workspace_e2e=tmp_path / "e2e", auth_dir=tmp_path / "auth", raw_dir=tmp_path / "raw",
                          spec_dirs=[tmp_path / "regressions" / "0007"])
    assert document["projects"] == [{"name": "regress-anonymous", "kind": "regress", "role": "anonymous",
                                     "testDir": str(tmp_path / "regressions" / "0007"),
                                     "testMatch": ["**/*.spec.ts"]}]


def test_common_parameters(tmp_path):
    document = build(tmp_path)
    assert (document["retries"], document["locale"]) == (2, "zh-CN")
    assert document["baseURL"] == "https://staging.example.test"
    assert document["outputDir"] == str(tmp_path / "raw" / "test-results")
    assert document["resultsFile"] == str(tmp_path / "raw" / "results.ndjson")
    assert document["htmlDir"] == str(tmp_path / "raw" / "html")
    assert document["loginModule"] == str(tmp_path / "e2e" / "login.ts")
    assert document["ignoreRequests"] == [{"method": "GET", "pathPattern": "^/api/Admin/", "status": 403}]
    assert "password" not in json.dumps(document).lower()


def test_spec_dirs_replace_the_patrol_cases(tmp_path):
    single = build(tmp_path, [tmp_path / "regressions" / "0007"])
    assert plan.selected_projects(single) == ["regress-Company", "regress-Personal"]
    assert single["projects"][1]["testDir"] == str(tmp_path / "regressions" / "0007")
    double = build(tmp_path, [tmp_path / "a", tmp_path / "b"])
    assert plan.selected_projects(double) == ["regress-Company-1", "regress-Company-2", "regress-Personal-1",
                                              "regress-Personal-2"]


def test_grep_from_config_and_caller(make_config):
    config = make_config(checks={"pages": {"patrolGrep": "@browse"}})
    assert plan.grep_for(config, None) == "@browse"
    assert plan.grep_for(config, "计算任务") == "计算任务"
    assert plan.grep_for(make_config(), None) is None


def test_plan_from_config_and_write(tmp_path, make_config):
    config = make_config(checks={"pages": {"locale": "ja-JP"}})
    document = plan.from_config(config, base_url="http://h", accounts={"Company": "c"}, workspace_e2e=tmp_path,
                                auth_dir=tmp_path, raw_dir=tmp_path)
    assert document["locale"] == "ja-JP" and document["ignoreRequests"] == []
    assert plan.from_config(make_config(), base_url="http://h", accounts={}, workspace_e2e=tmp_path,
                            auth_dir=tmp_path, raw_dir=tmp_path)["locale"] == "zh-CN"
    path = plan.write(tmp_path / "raw" / "page-plan.json", document)
    assert json.loads(path.read_text(encoding="utf-8")) == document


def test_command_and_environment(tmp_path, make_config):
    assert invoke.build_argv(["patrol-Company", "patrol-Personal"], "@browse", "@write") == (
        "npx", "playwright", "test", "--config", "playwright.config.ts", "--project", "patrol-Company",
        "--project", "patrol-Personal", "--grep", "@browse", "--grep-invert", "@write")
    assert invoke.build_argv(["patrol-Company"], None, None)[-2:] == ("--project", "patrol-Company")
    env = invoke.run_env({"PATH": "/bin", "SECRET_KEY": "x"}, tmp_path / "plan.json", {"Company": "pw"})
    assert env["TIGHTREIN_PAGE_PLAN"] == str(tmp_path / "plan.json") and env["TIGHTREIN_PASSWORD_Company"] == "pw"
    assert "SECRET_KEY" not in env
    assert invoke.timeout_seconds(make_config()) == 1800
    assert invoke.timeout_seconds(make_config(checks={"pages": {"timeoutMinutes": 2}})) == 120


def test_installation_check(tmp_path):
    assert "npm ci" in invoke.installed_problem(tmp_path)
    package = tmp_path / "node_modules" / "@playwright" / "test" / "package.json"
    package.parent.mkdir(parents=True)
    package.write_text("{}", encoding="utf-8")
    assert invoke.installed_problem(tmp_path) is None
    assert invoke.RUNTIME_DIR == Path(invoke.__file__).parent / "runtime"
