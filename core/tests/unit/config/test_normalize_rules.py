import pytest

from tightrein.config import normalize
from tightrein.config.project import ConfigError
from tightrein.domain.normalize import Rule


def test_missing_file_has_no_rules(tmp_path):
    assert normalize.load(tmp_path / "normalize.yaml") == ()


def test_rules_are_read_in_order(tmp_path):
    path = tmp_path / "normalize.yaml"
    path.write_text("rules:\n  - {pattern: 'lot-\\d+', replacement: '<lot>'}\n  - {pattern: x, replacement: ''}\n",
                    encoding="utf-8")
    assert normalize.load(path) == (Rule(r"lot-\d+", "<lot>"), Rule("x", ""))


def test_all_problems_are_reported_with_keys(tmp_path):
    path = tmp_path / "normalize.yaml"
    path.write_text("rules:\n  - {pattern: '(', replacement: a}\n  - {pattern: a}\n  - {pattern: '', replacement: 1}\n",
                    encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        normalize.load(path)
    assert [issue.key for issue in caught.value.issues] == [
        "rules[0].pattern", "rules[1]", "rules[2].pattern", "rules[2].replacement"]


def test_wrong_top_level_is_rejected(tmp_path):
    path = tmp_path / "normalize.yaml"
    path.write_text("- a\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        normalize.load(path)
