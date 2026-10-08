import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tightrein.protocol.naming import FixedClock
from tightrein.store.db import open_database
from tightrein.store.files.layout import WorkspaceLayout

NOW = datetime(2026, 10, 7, 9, 30, tzinfo=UTC)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def layout(tmp_path: Path) -> WorkspaceLayout:
    return WorkspaceLayout(tmp_path / "workspaces" / "demo")


@pytest.fixture
def db_path(layout: WorkspaceLayout) -> Path:
    return layout.database


@pytest.fixture
def conn(db_path: Path, clock: FixedClock) -> Iterator[sqlite3.Connection]:
    connection = open_database(db_path, clock=clock)
    yield connection
    connection.close()
