"""第 5 步写复现测试的参考：离根因文件最近的已有测试。"""

from tightrein.pipeline.fix.prompts.repro_test import siblings_text
from tightrein.pipeline.fix.steps import sibling_tests

PATTERNS = ("test_*.py", "*.test.js")
TRACKED = ("storage/database.py", "tests/test_database.py", "tests/test_exporter.py", "tests/storage/test_remote.py",
           "tests/js/api.test.js", "tests/conftest.py", "docs/test_notes.py")


def test_the_test_named_after_the_root_file_comes_first_then_shared_directories():
    found = sibling_tests.find(TRACKED, ["storage/database.py"], ["tests/"], PATTERNS, 2)
    assert found == ["tests/test_database.py", "tests/storage/test_remote.py"]
    assert sibling_tests.find(TRACKED, [], ["tests/"], PATTERNS, 1) == ["tests/js/api.test.js"]
    assert sibling_tests.find(TRACKED, ["storage/database.py"], ["tests/"], PATTERNS, 0) == []


def test_the_excerpt_keeps_the_header_and_the_whole_first_test():
    text = ("import pytest\nfrom storage.database import Database\n\n@pytest.fixture\ndef db(tmp_path):\n"
            "    return Database(tmp_path / 'x.db')\n\ndef test_first(db):\n    assert db\n\ndef test_second(db):\n"
            "    assert False\n")
    assert sibling_tests.excerpt(text, 80).endswith("def test_first(db):\n    assert db")
    assert sibling_tests.excerpt(text, 2) == "import pytest\nfrom storage.database import Database"


def test_siblings_are_read_and_quoted_in_the_prompt(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_database.py").write_text("import pytest\n\ndef test_a():\n    pass\n", encoding="utf-8")
    found = sibling_tests.read(tmp_path, ["tests/test_database.py", "tests/missing.py"], 80)
    assert [item.path for item in found] == ["tests/test_database.py"]
    text = siblings_text(found)
    assert "相邻的已有测试" in text and "`tests/test_database.py`" in text and "def test_a():" in text
    assert siblings_text([]) == ""
