from datetime import datetime, timezone

import pytest

from tightrein.domain.clock import FixedClock
from tightrein.store.migrations.runner import open_database

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "data" / "tightrein.db"


@pytest.fixture
def conn(db_path, clock):
    connection = open_database(db_path, clock)
    yield connection
    connection.close()
