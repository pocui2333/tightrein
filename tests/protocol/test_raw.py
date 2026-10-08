import pytest

from tightrein.protocol.raw import RawDir, raw_dir
from tightrein.store.files.layout import WorkspaceLayout


def test_raw_dir_paths(tmp_path):
    raw = RawDir(tmp_path / "raw")
    raw.write_json("Company/summary.json", {"a": 1})
    raw.write_text("notes.txt", "x")
    assert raw.files() == ("Company/summary.json", "notes.txt")
    for bad in ("", "/etc/passwd", "../x", "a/../../x", "a\\b"):
        with pytest.raises(ValueError):
            raw.path(bad)


def test_each_source_has_its_own_raw_dir(tmp_path):
    layout = WorkspaceLayout(tmp_path)
    found = raw_dir(layout, "R-20261005T030000Z-collect", "collect.platform_errors")
    assert found == layout.run_dir("R-20261005T030000Z-collect") / "12-collect.platform_errors-raw"
