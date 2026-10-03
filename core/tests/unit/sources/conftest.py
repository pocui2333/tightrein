import pytest
from probe_world import open_db


@pytest.fixture
def conn(tmp_path):
    connection = open_db(tmp_path)
    yield connection
    connection.close()
