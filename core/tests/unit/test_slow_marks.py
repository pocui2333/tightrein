from pathlib import Path

from slow_marks import apply, is_slow

ROOT = Path("/work/core/tests/unit")


class FakeItem:
    def __init__(self, path, name, fixtures=(), params=""):
        self.path = path
        self.originalname = name
        self.name = f"{name}{params}"
        self.fixturenames = list(fixtures)
        self.markers = []

    def add_marker(self, marker):
        self.markers.append(marker.name)


def test_real_git_repositories_and_registered_tests_are_slow():
    assert is_slow("vcs/test_git_read.py::test_head_of_a_repository", ["repos", "tmp_path"])
    assert is_slow("store/test_locks.py::test_process_alive", [])
    assert not is_slow("store/test_locks.py::test_acquire_a_free_lock", ["conn", "clock"])


def test_marks_follow_the_registered_names_without_parameters():
    items = [FakeItem(ROOT / "store" / "test_locks.py", "test_process_alive", params="[x]"),
             FakeItem(ROOT / "store" / "test_locks.py", "test_acquire_a_free_lock"),
             FakeItem(Path("/elsewhere/test_locks.py"), "test_process_alive")]
    apply(items, ROOT)
    assert [item.markers for item in items] == [["slow"], [], []]
