import pytest

from tightrein.config import layers
from tightrein.store import retention
from tightrein.store.files.layout import WorkspaceLayout, rotated_log
from tightrein.store.retention import rotate_launchd_logs, rotate_log


def write(path, size, fill="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fill * size, encoding="utf-8")


def test_default_limits():
    assert retention.RetentionPolicy().fixes_days == 90
    assert layers.core_value("runtime.store.launchdLogMaxBytes") == 10 * 1024 * 1024
    assert layers.core_value("runtime.store.launchdLogBackups") == 3


def test_rotated_names(tmp_path):
    path = tmp_path / "launchd.out.log"
    assert rotated_log(path, 1) == tmp_path / "launchd.out.log.1"
    with pytest.raises(ValueError):
        rotated_log(path, 0)


def test_small_or_missing_files_are_left_alone(tmp_path):
    path = tmp_path / "launchd.out.log"
    assert rotate_log(path, max_bytes=10) is False
    write(path, 9)
    assert rotate_log(path, max_bytes=10) is False
    assert sorted(item.name for item in tmp_path.iterdir()) == ["launchd.out.log"]


def test_a_full_file_is_moved_to_the_first_backup(tmp_path):
    path = tmp_path / "launchd.out.log"
    write(path, 10)
    assert rotate_log(path, max_bytes=10) is True
    assert not path.exists()
    assert rotated_log(path, 1).read_text(encoding="utf-8") == "x" * 10


def test_only_the_configured_number_of_backups_is_kept(tmp_path):
    path = tmp_path / "launchd.err.log"
    for fill in "abcde":
        write(path, 10, fill)
        rotate_log(path, max_bytes=10, backups=3)
    assert sorted(item.name for item in tmp_path.iterdir()) == [
        "launchd.err.log.1", "launchd.err.log.2", "launchd.err.log.3",
    ]
    assert [rotated_log(path, number).read_text(encoding="utf-8")[0] for number in (1, 2, 3)] == ["e", "d", "c"]


def test_gaps_in_backups_are_tolerated(tmp_path):
    path = tmp_path / "launchd.out.log"
    write(rotated_log(path, 2), 1, "b")
    write(path, 10, "n")
    rotate_log(path, max_bytes=10, backups=3)
    assert sorted(item.name for item in tmp_path.iterdir()) == ["launchd.out.log.1", "launchd.out.log.3"]


def test_invalid_limits(tmp_path):
    with pytest.raises(ValueError):
        rotate_log(tmp_path / "a.log", max_bytes=0)
    with pytest.raises(ValueError):
        rotate_log(tmp_path / "a.log", backups=0)


def test_launchd_logs_of_a_workspace(tmp_path):
    layout = WorkspaceLayout(tmp_path / "workspaces" / "sample")
    write(layout.launchd_out_log(), 20)
    write(layout.launchd_err_log(), 5)
    assert rotate_launchd_logs(layout, max_bytes=10) == (layout.launchd_out_log(),)
    assert rotated_log(layout.launchd_out_log(), 1).is_file()
    assert layout.launchd_err_log().is_file()
