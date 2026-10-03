import pytest
import yaml

from tightrein.config import edit

TEXT = """# 项目说明
project:
  name: sample   # 名称
git:
  personalPrefix: true
  conventions:
    branch: old
    commit: old

checks:
  commands:
    - {name: unit, cwd: ., command: make test}
"""


def test_a_nested_key_is_replaced_and_comments_are_kept(tmp_path):
    path = tmp_path / "project.yaml"
    path.write_text(TEXT, encoding="utf-8")
    edit.set_value(path, "git.conventions", {"branch": "{type}/{issue}-{slug}", "commit": "{type}: {summary}"})
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert data["git"] == {"personalPrefix": True,
                           "conventions": {"branch": "{type}/{issue}-{slug}", "commit": "{type}: {summary}"}}
    assert "# 名称" in text and data["checks"]["commands"][0]["name"] == "unit"


def test_missing_sections_and_keys_are_added(tmp_path):
    path = tmp_path / "project.yaml"
    path.write_text(TEXT, encoding="utf-8")
    edit.set_value(path, "extensions.deploy-source", {"use": "core/github-actions", "options": {"workflow": "d.yml"}})
    edit.set_value(path, "project.language", "zh")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["extensions"]["deploy-source"]["options"]["workflow"] == "d.yml"
    assert data["project"] == {"name": "sample", "language": "zh"}


def test_flow_style_sections_are_left_to_the_user(tmp_path):
    path = tmp_path / "project.yaml"
    path.write_text("git: {personalPrefix: true}\n", encoding="utf-8")
    with pytest.raises(edit.EditRejected):
        edit.set_value(path, "git.conventions", {"branch": "x"})
    assert path.read_text(encoding="utf-8") == "git: {personalPrefix: true}\n"
