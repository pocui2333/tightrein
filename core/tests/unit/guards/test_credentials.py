import pytest

from tightrein.domain.enums import ViolationKind
from tightrein.guards.credentials import build_env, config_credentials, credential_files
from tightrein.vcs.git_read import GitReader
from tightrein.vcs.process import VcsProcess

BASE = {
    "PATH": "/usr/local/bin:/usr/bin", "HOME": "/Users/cty", "USER": "cty", "LANG": "zh_CN.UTF-8",
    "LC_ALL": "zh_CN.UTF-8", "TERM": "xterm-256color", "SHELL": "/bin/zsh", "TMPDIR": "/var/folders/x/T/",
    "GH_TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz0123", "ANTHROPIC_API_KEY": "sk-ant-abcdefghijklmnopqrstu",
    "MY_SERVICE_PASSWORD": "Pa55-w0rd!", "EDITOR": "vim", "LC_SESSION_ID": "42",
    "LOGNAME": "bearer abcdefghijklmnop", "CODEX_HOME": "/Users/cty/.codex",
}


def test_only_whitelisted_non_credential_variables_are_kept():
    agent = build_env(BASE)
    assert agent.env == {
        "PATH": "/usr/local/bin:/usr/bin", "HOME": "/Users/cty", "USER": "cty", "LANG": "zh_CN.UTF-8",
        "LC_ALL": "zh_CN.UTF-8", "TERM": "xterm-256color", "SHELL": "/bin/zsh", "TMPDIR": "/var/folders/x/T/",
        "GIT_TERMINAL_PROMPT": "0",
    }
    assert agent.removed_names == ("ANTHROPIC_API_KEY", "GH_TOKEN", "LC_SESSION_ID", "LOGNAME", "MY_SERVICE_PASSWORD")


def test_adapter_variables_and_readonly_locks():
    agent = build_env(BASE, extra_names=["CODEX_HOME", "GH_TOKEN"], readonly=True)
    assert agent.env["CODEX_HOME"] == "/Users/cty/.codex"
    assert "GH_TOKEN" not in agent.env
    assert agent.env["GIT_OPTIONAL_LOCKS"] == "0"


def test_proxy_variables_are_kept_unless_they_carry_credentials():
    agent = build_env({**BASE, "https_proxy": "http://127.0.0.1:8118", "NO_PROXY": "github.com",
                       "ALL_PROXY": "socks5://user:secret@10.0.0.1:1080"})
    assert (agent.env["https_proxy"], agent.env["NO_PROXY"]) == ("http://127.0.0.1:8118", "github.com")
    assert "ALL_PROXY" not in agent.env and "ALL_PROXY" in agent.removed_names


@pytest.fixture
def clone(repos):
    _, repo = repos.origin_and_clone()
    return repos, repo, GitReader(VcsProcess(environ=repos.environ))


def test_untracked_credential_files_are_found_even_when_ignored(clone):
    repos, repo, git = clone
    repos.write(repo, ".gitignore", "bin/\nobj/\n.env\n")
    repos.write(repo, ".env", "DB_PASSWORD=x\n")
    repos.write(repo, "certs/server.pem", "-----BEGIN PRIVATE KEY-----\n")
    repos.write(repo, "src/appsettings.json", "{}\n")
    found = credential_files(git, repo, [".env", "*.pem", "secrets*.json"])
    assert [(item.kind, item.path) for item in found] == [
        (ViolationKind.CREDENTIAL_PRESENT, ".env"), (ViolationKind.CREDENTIAL_PRESENT, "certs/server.pem"),
    ]


def test_tracked_files_are_not_reported(clone):
    repos, repo, git = clone
    repos.commit(repo, "chore: sample env", {".env.example": "DB_PASSWORD=\n"})
    assert credential_files(git, repo, [".env*"]) == []


def test_remote_urls_with_credentials_and_helpers(clone):
    repos, repo, git = clone
    assert config_credentials(git, repo) == []
    repos.git(repo, "remote", "add", "mirror", "https://cty:ghp_abcdefghijklmnopqrstuvwxyz0123@github.com/o/r.git")
    repos.git(repo, "config", "credential.helper", "store")
    found = config_credentials(git, repo)
    assert [item.kind for item in found] == [ViolationKind.CREDENTIAL_PRESENT] * 2
    assert "credential.helper" in found[0].detail
    assert "ghp_" not in found[1].detail
    assert "remote.mirror.url" in found[1].detail


def test_token_as_user_name_is_a_credential(clone):
    repos, repo, git = clone
    repos.git(repo, "remote", "set-url", "origin", "https://ghp_abcdefghijklmnopqrstuvwxyz0123@github.com/o/r.git")
    assert len(config_credentials(git, repo)) == 1
    repos.git(repo, "remote", "set-url", "origin", "git@github.com:o/r.git")
    assert config_credentials(git, repo) == []
