import pytest
from eval_world import EvalWorld


@pytest.fixture
def world(tmp_path, repos):
    created = EvalWorld(tmp_path, repos)
    yield created
    created.conn.close()
