import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tightrein.protocol.git import GitError
from tightrein.protocol.git.git import Commit, Head
from tightrein.protocol.naming import FixedClock
from tightrein.store.files.layout import ToolLayout

NOW = datetime(2026, 10, 8, 1, 0, tzinfo=UTC)
DEFAULTS = Path(__file__).resolve().parents[2] / "settings" / "defaults.json"


class FakeGit:
    """detect 只用到这几个只读方法。"""

    def __init__(self, remote: list[str] | None = None, branch: str | None = "dev", url: str | None = None,
                 broken: bool = False, subjects: list[str] | None = None) -> None:
        self.remote = remote or []
        self.branch = branch
        self.url = url
        self.broken = broken
        self.subjects = subjects or []

    def log(self, rev_range: str, paths: list[str] | None = None, limit: int | None = None) -> list[Commit]:
        if self.broken:
            raise GitError("git 不可用")
        return [Commit(f"{number:040x}", "dev", NOW, subject) for number, subject in enumerate(self.subjects[:limit])]

    def remote_branches(self) -> list[str]:
        if self.broken:
            raise GitError("git 不可用")
        return self.remote

    def head(self) -> Head:
        return Head("a" * 40, self.branch)

    def remote_url(self, remote: str = "origin") -> str | None:
        return self.url


@pytest.fixture
def fake_git() -> type[FakeGit]:
    return FakeGit


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def tool(tmp_path: Path) -> ToolLayout:
    layout = ToolLayout(tmp_path / "tool")
    layout.settings_dir.mkdir(parents=True)
    shutil.copy(DEFAULTS, layout.defaults)
    return layout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "shop"
    (path / ".git").mkdir(parents=True)
    return path
