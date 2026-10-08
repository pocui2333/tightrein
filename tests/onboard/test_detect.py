import json
from pathlib import Path

from tightrein.onboard.detect import convention_lines, detect, hints, settings_draft, setup_draft
from tightrein.onboard.setup import FIELDS, MODULES


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_node_commands_come_from_package_scripts_and_the_lockfile_manager(repo, fake_git):
    write(repo / "package.json", json.dumps({
        "scripts": {"test": "vitest", "lint": "eslint .", "build": "vite build", "type-check": "tsc"},
        "dependencies": {"react": "19", "@sentry/react": "8"}}))
    write(repo / "pnpm-lock.yaml", "")
    found = detect(repo, fake_git(remote=["main", "feat/x"]))
    assert found.commands == {"test": "pnpm test", "lint": "pnpm run lint", "build": "pnpm run build",
                              "typecheck": "pnpm run type-check"}
    assert found.main_branch == "main"
    assert found.sentry == ["package.json"]
    assert found.frontend_patterns and "**/*.test.*" in found.test_patterns


def test_python_commands_follow_the_tool_configuration_and_make_targets_win(repo, fake_git):
    write(repo / "pyproject.toml", '[project]\nname = "x"\n[tool.ruff]\nline-length = 100\n[tool.mypy]\n'
                                   '[tool.pytest.ini_options]\naddopts = "-q"\n')
    write(repo / "Makefile", "test:\n\tpytest\nlint :\n\truff check\nX := 1\n")
    found = detect(repo, fake_git())
    assert found.commands == {"test": "make test", "lint": "make lint", "build": None, "typecheck": "mypy ."}
    assert found.stacks == {"python": ["pyproject.toml"]}


def test_python_tests_run_with_the_project_venv_or_python3(repo, fake_git):
    write(repo / "requirements.txt", "pytest\n")
    assert detect(repo, fake_git()).commands["test"] == "python3 -m pytest -q"
    write(repo / ".venv" / "bin" / "python", "")
    expected = f"{(repo / '.venv' / 'bin' / 'python').absolute()} -m pytest -q"
    assert detect(repo, fake_git()).commands["test"] == expected


def test_what_cannot_be_detected_is_null(repo, fake_git):
    found = detect(repo, fake_git(broken=True))
    assert found.main_branch is None and found.conventions is None
    assert found.notes == ["读取主分支失败：git 不可用", "从提交历史推断约定失败：git 不可用"]
    assert found.commands == {"test": None, "lint": None, "build": None, "typecheck": None}
    assert found.spec is None and found.sentry == [] and found.ci == []


def test_main_branch_falls_back_to_the_current_branch(repo, fake_git):
    assert detect(repo, fake_git(remote=["develop"], branch="trunk")).main_branch == "trunk"
    assert detect(repo, fake_git(remote=["develop", "master"])).main_branch == "master"


def test_spec_ci_and_deploy_workflows_are_found(repo, fake_git):
    write(repo / "docs" / "openapi.yaml", "openapi: 3.1.0\n")
    write(repo / ".github" / "workflows" / "ci.yml", "")
    write(repo / ".github" / "workflows" / "deploy-prod.yml", "")
    write(repo / ".gitlab-ci.yml", "")
    found = detect(repo, fake_git(url="git@github.com:acme/shop.git"))
    assert found.spec == "docs/openapi.yaml"
    assert found.ci == [".github/workflows/ci.yml", ".github/workflows/deploy-prod.yml", ".gitlab-ci.yml"]
    assert found.deploy_workflows == [".github/workflows/deploy-prod.yml"]
    assert found.github_remote
    assert hints(found) == {"collect.api_fuzz": "接口描述：docs/openapi.yaml",
                            "release.deploy": "部署工作流：.github/workflows/deploy-prod.yml"}


def test_the_setup_draft_lists_every_module_with_every_field(repo, fake_git):
    write(repo / "requirements.txt", "sentry-sdk==2\n")
    found = detect(repo, fake_git(url="https://github.com/acme/shop"))
    draft = setup_draft(found, "shop", "2026-10-08T01:00:00Z")
    assert list(draft["modules"]) == list(MODULES)
    assert all(list(entry) == list(FIELDS) for entry in draft["modules"].values())
    assert draft["modules"]["collect.platform_errors"]["status"] == "enabled"
    assert draft["modules"]["collect.platform_errors"]["method"] == "sentry"
    assert draft["modules"]["release.github_issues"]["status"] == "enabled"
    assert draft["modules"]["collect.alerts"]["status"] is None


def test_the_settings_draft_has_every_project_fact(repo, fake_git):
    settings = settings_draft(detect(repo, fake_git(remote=["main"])), "en")
    assert settings["project"] == {"repo": str(repo.resolve()), "mainBranch": "main", "language": "en",
                                   "commands": {"test": None, "lint": None, "build": None, "typecheck": None},
                                   "testPatterns": None, "frontendPatterns": None}
    assert settings["overrides"] == {}


def test_conventions_inferred_from_history_are_only_reported(repo, fake_git):
    subjects = [f"feat(api): change {number}" for number in range(9)] + ["Merge branch 'x'", "fix: typo"]
    branches = ["main"] + [f"feat/{number}-thing" for number in range(10)]
    found = detect(repo, fake_git(remote=branches, subjects=subjects))
    assert found.conventions is not None and found.conventions.commit.format == "{type}{scope}: {summary}"
    lines = convention_lines(found.conventions)
    assert lines[0].startswith("提交：`{type}{scope}: {summary}`(占 100%，例：feat(api): change 0")
    assert lines[0].endswith("要采用就写进 settings.json 的 overrides.git.commit，程序不自动采用")
    assert lines[1].startswith("分支：")
    # 只报告：不写进 settings 草稿
    assert "git" not in settings_draft(found, "zh")["overrides"]
    assert convention_lines(None) == []
