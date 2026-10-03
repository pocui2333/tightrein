from tightrein.sources.static.baseline import ROOT_MODULE, BaselineSettings, plan

EXCLUDE = ("fixtures/", "*.lock", "*.md", "dist/")


def settings(files=3, lines=100, exclude=EXCLUDE):
    return BaselineSettings(max_claims=5, batch_files=files, batch_lines=lines, exclude=exclude)


def write(root, files):
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return sorted(files)


def lines(count):
    return "".join(f"line {number}\n" for number in range(count))


def test_excludes_patterns_binaries_and_empty_files(tmp_path):
    files = write(tmp_path, {
        "src/app.py": lines(5), "src/fixtures/case.json": lines(2), "poetry.lock": lines(9), "README.md": lines(3),
        "dist/bundle.js": lines(4), "src/logo.bin": b"\x89PNG\0\0data", "src/__init__.py": "",
        "src/tail.py": "no newline"})
    found = plan(tmp_path, files, settings())
    assert found.excluded == 6
    assert [[(item.path, item.lines) for item in batch.files] for batch in found.batches] == [
        [("src/app.py", 5), ("src/tail.py", 1)]]


def test_small_modules_share_a_batch_within_the_limits(tmp_path):
    files = write(tmp_path, {
        "api/routes/a.py": lines(10), "api/routes/b.py": lines(10), "api/store/c.py": lines(10),
        "web/main.ts": lines(10), "setup.py": lines(10)})
    found = plan(tmp_path, files, settings())
    assert [(batch.module, batch.paths) for batch in found.batches] == [
        ("api", ("api/routes/a.py", "api/routes/b.py", "api/store/c.py")),
        (ROOT_MODULE, ("setup.py", "web/main.ts")),
    ]
    assert [(batch.number, batch.total) for batch in found.batches] == [(1, 2), (2, 2)]


def test_large_directories_are_split_and_oversized_files_stand_alone(tmp_path):
    files = write(tmp_path, {
        "core/a.py": lines(40), "core/b.py": lines(40), "core/c.py": lines(40), "core/d.py": lines(150),
        "core/e.py": lines(10), "core/util/f.py": lines(10)})
    found = plan(tmp_path, files, settings())
    assert [batch.paths for batch in found.batches] == [
        ("core/a.py", "core/b.py"), ("core/c.py",), ("core/d.py",), ("core/e.py",), ("core/util/f.py",)]
    assert found.batches[2].lines == 150
    assert found.batches[0].describe() == "第 1/5 批(core，2 个文件、80 行)"


def test_settings_come_from_the_layered_config(make_config):
    loaded = BaselineSettings.from_config(make_config(sources={"static": {"baseline": {"batchFiles": 7,
                                                                                     "exclude+": ["tests/"]}}}))
    assert (loaded.batch_files, loaded.max_claims, loaded.batch_lines) == (7, 40, 3000)
    assert "*.lock" in loaded.exclude and loaded.exclude[-1] == "tests/"
