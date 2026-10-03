"""按改动的文件挑选要运行的单元测试。

用法(在 core 目录)：
    .venv/bin/python dev/affected_tests.py [--run] [文件 ...]
文件为相对仓库根目录或当前目录的路径；没有给出时读取 `git diff --name-only HEAD` 与
`git ls-files --others --exclude-standard` 的结果。选中的测试：
1. 镜像目录：core/tightrein/<包>/ 下有改动时，core/tests/unit/<包>/ 下的全部测试；
2. 反向导入：扫描 core/tightrein 与 core/tests 中的导入语句，从改动的模块出发，沿「被谁导入」传递，
   途经的 core/tests/unit 下的测试模块都选中(测试辅助模块、改动的测试文件本身同样按此处理)；
   改动的 conftest.py 选中它所在目录；
3. 上一次运行失败的测试(pytest 缓存中的 lastfailed，相当于 `--lf` 的范围)。
默认打印 pytest 命令；--run 时以 -q 运行并返回 pytest 的退出码。
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

CORE = Path(__file__).resolve().parents[1]
PACKAGE = "tightrein"
UNIT = Path("tests") / "unit"
LAST_FAILED = Path(".pytest_cache") / "v" / "cache" / "lastfailed"


def module_name(path: Path, core: Path) -> str | None:
    """core 下的 Python 文件对应的模块名：tightrein 下为包路径，tests 下为文件名(测试以 sys.path 导入同目录的模块)。"""
    relative = path.relative_to(core)
    if relative.parts[0] == PACKAGE:
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)
    if relative.parts[0] == "tests":
        return relative.stem
    return None


def imported_names(path: Path, current: str) -> set[str]:
    """文件中导入的模块名；相对导入按当前模块换算为绝对名。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = current.split(".")[:-node.level]
                base = ".".join([*parent, base] if base else parent)
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def resolve_import(name: str, importer: Path, core: Path, packaged: dict[str, Path]) -> Path | None:
    """导入名对应的 core 中的文件：tightrein 的模块按包路径，其他名字在导入方所在目录及其上层的 tests 目录中找。"""
    if name in packaged:
        return packaged[name]
    if "." in name:
        return None
    directory = importer.parent
    while core / "tests" in (directory, *directory.parents):
        candidate = directory / f"{name}.py"
        if candidate.is_file():
            return candidate
        directory = directory.parent
    return None


def reverse_imports(core: Path) -> dict[Path, set[Path]]:
    """文件 → 直接导入它的文件。"""
    files = [path for root in (core / PACKAGE, core / "tests") for path in root.rglob("*.py")]
    packaged = {module_name(path, core): path for path in files if path.is_relative_to(core / PACKAGE)}
    importers: dict[Path, set[Path]] = defaultdict(set)
    for path in files:
        for name in imported_names(path, module_name(path, core) or ""):
            target = resolve_import(name, path, core, packaged)
            if target is not None and target != path:
                importers[target].add(path)
    return importers


def is_test(path: Path, core: Path) -> bool:
    return path.is_relative_to(core / UNIT) and path.name.startswith("test_") and path.suffix == ".py"


def select(changed: list[Path], core: Path = CORE) -> list[str]:
    """要运行的测试，路径相对 core，排好序。"""
    selected: set[Path] = set()
    unit = core / UNIT
    for path in changed:
        relative = path.relative_to(core)
        if relative.parts[0] == PACKAGE and len(relative.parts) > 2 and (unit / relative.parts[1]).is_dir():
            selected.add(unit / relative.parts[1])
        if path.name == "conftest.py" and path.is_relative_to(unit):
            selected.add(path.parent)
    importers = reverse_imports(core)
    pending = [path for path in changed if path.suffix == ".py"]
    seen = set(pending)
    while pending:
        current = pending.pop()
        if is_test(current, core) and current.is_file():
            selected.add(current)
        for importer in importers.get(current, ()):
            if importer not in seen:
                seen.add(importer)
                pending.append(importer)
    selected |= last_failed(core)
    covered = {path for path in selected if path.is_dir()}
    kept = {path for path in selected if not any(parent in covered for parent in path.parents)}
    return sorted(str(path.relative_to(core)) for path in kept)


def last_failed(core: Path) -> set[Path]:
    try:
        failed = json.loads((core / LAST_FAILED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    files = {core / nodeid.split("::")[0] for nodeid in failed}
    return {path for path in files if path.is_file() and path.is_relative_to(core / UNIT)}


def changed_files(arguments: list[str], core: Path = CORE) -> list[Path]:
    """参数给出的文件(以 core/ 开头的相对仓库根目录，其余相对当前目录)，或 git 报告的改动与未跟踪文件；
    只保留 core 下的文件。"""
    root = core.parent
    if arguments:
        paths = [root / name if name.startswith(f"{core.name}/") else Path.cwd() / name for name in arguments]
    else:
        diff = subprocess.run(["git", "diff", "--name-only", "HEAD"], cwd=root, capture_output=True, text=True,
                              check=True).stdout
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=root,
                                   capture_output=True, text=True, check=True).stdout
        paths = [root / line for line in (diff + untracked).splitlines() if line.strip()]
    resolved = [path.resolve() for path in paths]
    return [path for path in resolved if path.is_relative_to(core)]


def main(argv: list[str]) -> int:
    run = "--run" in argv
    arguments = [argument for argument in argv if argument != "--run"]
    tests = select(changed_files(arguments))
    if not tests:
        print("没有受影响的单元测试")
        return 0
    command = [sys.executable, "-m", "pytest", "-q", *tests]
    print(" ".join(command))
    if not run:
        return 0
    code = subprocess.run(command, cwd=CORE, check=False).returncode
    print(f"pytest 退出码 {code}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
