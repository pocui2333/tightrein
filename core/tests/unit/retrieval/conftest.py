import pytest
from knowledge_world import KnowledgeWorld


@pytest.fixture
def world(tmp_path):
    created = KnowledgeWorld(tmp_path)
    yield created
    created.close()
