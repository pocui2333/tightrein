import importlib.util
import json
from pathlib import Path

TOOL = Path(__file__).resolve().parents[2] / "dev" / "affected_tests.py"
SPEC = importlib.util.spec_from_file_location("affected_tests", TOOL)
affected_tests = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(affected_tests)


def write(root, relative, text=""):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_core(root):
    core = root / "core"
    for relative, text in {
        "tightrein/__init__.py": "",
        "tightrein/alpha/__init__.py": "",
        "tightrein/alpha/base.py": "VALUE = 1\n",
        "tightrein/beta/__init__.py": "",
        "tightrein/beta/user.py": "from tightrein.alpha.base import VALUE\n",
        "tightrein/beta/relative.py": "from . import user\n",
        "tests/unit/alpha/test_base.py": "",
        "tests/unit/gamma/gamma_world.py": "from tightrein.beta import relative\n",
        "tests/unit/gamma/test_gamma.py": "from gamma_world import VALUE\n",
        "tests/unit/delta/test_delta.py": "import os\n",
    }.items():
        write(core, relative, text)
    return core


def test_mirrored_packages_and_modules_that_import_the_change_are_selected(tmp_path):
    core = make_core(tmp_path)
    assert affected_tests.select([core / "tightrein/alpha/base.py"], core) == [
        "tests/unit/alpha", "tests/unit/gamma/test_gamma.py"]
    assert affected_tests.select([core / "tests/unit/gamma/gamma_world.py"], core) == ["tests/unit/gamma/test_gamma.py"]
    assert affected_tests.select([core / "tests/unit/delta/conftest.py"], core) == ["tests/unit/delta"]


def test_last_failed_tests_are_added(tmp_path):
    core = make_core(tmp_path)
    failed = {"tests/unit/delta/test_delta.py::test_x": True, "tests/unit/removed/test_old.py::test_y": True}
    write(core, ".pytest_cache/v/cache/lastfailed", json.dumps(failed))
    assert affected_tests.select([], core) == ["tests/unit/delta/test_delta.py"]


def test_arguments_are_taken_from_the_repository_root_or_the_current_directory(tmp_path, monkeypatch):
    core = make_core(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert affected_tests.changed_files(["core/tightrein/alpha/base.py", "README.md"], core) == [
        core / "tightrein/alpha/base.py"]
    monkeypatch.chdir(core)
    assert affected_tests.changed_files(["tightrein/beta/user.py"], core) == [core / "tightrein/beta/user.py"]
