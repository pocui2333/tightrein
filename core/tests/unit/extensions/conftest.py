import pytest
from extension_world import ExtensionWorld


@pytest.fixture
def world(tmp_path, make_config):
    return ExtensionWorld(tmp_path, make_config)
