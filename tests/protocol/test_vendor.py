import io
import json
import tarfile
from pathlib import Path

import pytest

from tightrein.protocol import vendor
from tightrein.protocol.vendor import HashMismatch, LockedSkill, LockInvalid, VendorError
from tightrein.store.files.layout import ToolLayout

SOURCE = "https://github.com/example/skills"
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
REVIEW_DIR = "plugins/differential-review/skills/differential-review"
REPO_ROOT = Path(__file__).resolve().parents[2]


def skill_text(name: str, body: str = "正文。\n") -> str:
    return f"---\nname: {name}\ndescription: 测试\n---\n\n{body}"


def archive(files: dict[str, str], top: str = "skills-main") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def upstream(body: str = "正文。\n") -> bytes:
    return archive({
        f"{REVIEW_DIR}/SKILL.md": skill_text("differential-review", body),
        f"{REVIEW_DIR}/references/methodology.md": "方法。\n",
        "plugins/differential-review/README.md": "不属于 skill 目录。\n",
        "plugins/other/skills/other/SKILL.md": skill_text("other"),
    })


def write_files(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class Downloads:
    def __init__(self) -> None:
        self.archives = {COMMIT_A: upstream(), COMMIT_B: upstream("新的正文。\n")}
        self.urls: list[str] = []

    def __call__(self, source: str, commit: str) -> bytes:
        self.urls.append(vendor.archive_url(source, commit))
        return self.archives[commit]


def locked_skill(tmp_path: Path, downloads: Downloads) -> LockedSkill:
    staging = tmp_path / "staging"
    path = vendor.extract(downloads(SOURCE, COMMIT_A), "differential-review", None, staging)
    files = vendor.file_hashes(staging)
    downloads.urls.clear()
    return LockedSkill("differential-review", SOURCE, COMMIT_A, path, "CC-BY-SA-4.0", vendor.tree_hash(files), files,
                       "2026-10-07")


def test_the_tree_hash_ignores_order_permissions_and_git(tmp_path: Path) -> None:
    files = {"SKILL.md": "a\n", "references/x.md": "b\n"}
    write_files(tmp_path / "one", files)
    write_files(tmp_path / "two", dict(reversed(list(files.items()))))
    write_files(tmp_path / "two", {".git/HEAD": "ref\n"})
    (tmp_path / "two" / "SKILL.md").chmod(0o600)
    one, two = vendor.file_hashes(tmp_path / "one"), vendor.file_hashes(tmp_path / "two")
    assert one == two
    assert list(one) == ["SKILL.md", "references/x.md"]
    assert vendor.tree_hash(one) == vendor.tree_hash(two)
    assert vendor.tree_hash(one).startswith("sha256:")


def test_one_changed_byte_fails_verification(tmp_path: Path) -> None:
    write_files(tmp_path / "skill", {"SKILL.md": "a\n"})
    files = vendor.file_hashes(tmp_path / "skill")
    skill = LockedSkill("demo", SOURCE, COMMIT_A, "demo", "MIT", vendor.tree_hash(files), files, "2026-10-07")
    assert vendor.verify(skill, tmp_path / "skill") == []
    (tmp_path / "skill" / "SKILL.md").write_text("b\n", encoding="utf-8")
    (mismatch,) = vendor.verify(skill, tmp_path / "skill")
    assert (mismatch.path, mismatch.expected) == ("SKILL.md", files["SKILL.md"])
    assert mismatch.actual not in (None, mismatch.expected)
    write_files(tmp_path / "skill", {"SKILL.md": "a\n", "extra.sh": "echo\n"})
    (extra,) = vendor.verify(skill, tmp_path / "skill")
    assert (extra.path, extra.expected) == ("extra.sh", None)
    assert vendor.verify(skill, tmp_path / "gone")[0].actual is None


def test_load_refuses_a_changed_copy_and_names_the_file(tmp_path: Path) -> None:
    tool = ToolLayout(tmp_path)
    write_files(tool.vendor_skill("demo"), {"SKILL.md": "a\n"})
    files = vendor.file_hashes(tool.vendor_skill("demo"))
    vendor.write_lock(tool.vendor_lock, [
        LockedSkill("demo", SOURCE, COMMIT_A, "demo", "MIT", vendor.tree_hash(files), files, "2026-10-07"),
        LockedSkill("pending", SOURCE),
    ])
    assert vendor.load(tool, "demo") == tool.vendor_skill("demo")
    (tool.vendor_skill("demo") / "SKILL.md").write_text("b\n", encoding="utf-8")
    with pytest.raises(HashMismatch, match="SKILL.md：期望 "):
        vendor.load(tool, "demo")
    with pytest.raises(VendorError, match="尚未锁定"):
        vendor.load(tool, "pending")
    with pytest.raises(VendorError, match="没有 missing"):
        vendor.load(tool, "missing")


def test_extract_finds_the_skill_by_name_and_only_takes_its_directory(tmp_path: Path) -> None:
    path = vendor.extract(upstream(), "differential-review", None, tmp_path / "copy")
    assert path == REVIEW_DIR
    assert list(vendor.file_hashes(tmp_path / "copy")) == ["SKILL.md", "references/methodology.md"]
    assert not (tmp_path / "copy.partial").exists()


def test_extract_rejects_paths_outside_the_skill_directory(tmp_path: Path) -> None:
    escaping = archive({"skill/SKILL.md": skill_text("demo"), "skill/../escape.md": "x\n"})
    with pytest.raises(ValueError):
        vendor.extract(escaping, "demo", "skill", tmp_path / "copy")
    assert list(tmp_path.iterdir()) == []


def test_extract_needs_exactly_one_matching_skill(tmp_path: Path) -> None:
    with pytest.raises(VendorError, match="有 0 个"):
        vendor.extract(upstream(), "missing-skill", None, tmp_path / "copy")


def test_ensure_present_downloads_once_and_never_overwrites_a_tampered_copy(tmp_path: Path) -> None:
    downloads = Downloads()
    skill = locked_skill(tmp_path, downloads)
    directory = tmp_path / "vendor" / "skills" / "differential-review"
    assert vendor.ensure_present(skill, downloads, directory) == directory
    assert vendor.ensure_present(skill, downloads, directory) == directory
    assert downloads.urls == [f"https://codeload.github.com/example/skills/tar.gz/{COMMIT_A}"]
    (directory / "SKILL.md").write_text("改过\n", encoding="utf-8")
    with pytest.raises(HashMismatch):
        vendor.ensure_present(skill, downloads, directory)
    assert len(downloads.urls) == 1
    assert (directory / "SKILL.md").read_text(encoding="utf-8") == "改过\n"


def test_a_download_that_does_not_match_the_lock_is_removed(tmp_path: Path) -> None:
    downloads = Downloads()
    skill = locked_skill(tmp_path, downloads)
    downloads.archives[COMMIT_A] = upstream("被替换的正文。\n")
    directory = tmp_path / "vendor" / "skills" / "differential-review"
    with pytest.raises(HashMismatch):
        vendor.ensure_present(skill, downloads, directory)
    assert not directory.exists()


def test_update_replaces_the_copy_and_lists_the_changed_files(tmp_path: Path) -> None:
    downloads = Downloads()
    skill = locked_skill(tmp_path, downloads)
    directory = tmp_path / "vendor" / "skills" / "differential-review"
    vendor.ensure_present(skill, downloads, directory)
    updated, change = vendor.update(skill, COMMIT_B, downloads, directory, license="CC-BY-SA-4.0", today="2026-10-08")
    assert (updated.commit, updated.path, updated.locked_at) == (COMMIT_B, REVIEW_DIR, "2026-10-08")
    assert change.changed == ("SKILL.md",) and change.added == () and change.removed == ()
    assert vendor.verify(updated, directory) == []
    assert not directory.with_name("differential-review.new").exists()


def test_update_stops_when_the_current_copy_was_changed(tmp_path: Path) -> None:
    downloads = Downloads()
    skill = locked_skill(tmp_path, downloads)
    directory = tmp_path / "vendor" / "skills" / "differential-review"
    vendor.ensure_present(skill, downloads, directory)
    (directory / "SKILL.md").write_text("改过\n", encoding="utf-8")
    with pytest.raises(HashMismatch):
        vendor.update(skill, COMMIT_B, downloads, directory, license="CC-BY-SA-4.0", today="2026-10-08")
    assert (directory / "SKILL.md").read_text(encoding="utf-8") == "改过\n"


def test_the_lock_round_trips_through_json(tmp_path: Path) -> None:
    downloads = Downloads()
    skill = locked_skill(tmp_path, downloads)
    path = tmp_path / "lock.json"
    vendor.write_lock(path, [skill, LockedSkill("later", SOURCE)])
    assert vendor.read_lock(path) == [skill, LockedSkill("later", SOURCE)]
    assert vendor.read_lock(tmp_path / "missing.json") == []


@pytest.mark.parametrize("data, message", [
    ({"lockVersion": 2, "skills": []}, "lockVersion"),
    ({"lockVersion": 1, "skills": [{"name": "a", "source": SOURCE, "commit": "main"}]}, "skills[0].commit"),
    ({"lockVersion": 1, "skills": [{"name": "a", "source": SOURCE, "commit": COMMIT_A}]}, "skills[0].treeHash"),
    ({"lockVersion": 1, "skills": [{"name": "a", "source": SOURCE, "extra": 1}]}, "skills[0].extra"),
    ({"lockVersion": 1, "skills": [{"name": "A b", "source": SOURCE}]}, "skills[0].name"),
])
def test_an_invalid_lock_is_rejected_with_the_key(tmp_path: Path, data: dict, message: str) -> None:
    path = tmp_path / "lock.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(LockInvalid) as caught:
        vendor.read_lock(path)
    assert any(problem.startswith(message) for problem in caught.value.problems)


def test_the_repository_copies_match_their_lock() -> None:
    tool = ToolLayout(REPO_ROOT)
    skills = vendor.read_lock(tool.vendor_lock)
    assert [skill.name for skill in skills] == [
        "differential-review", "variant-analysis", "sharp-edges"]
    for skill in skills:
        assert skill.locked and skill.license == "CC-BY-SA-4.0"
        assert skill.tree_hash == vendor.tree_hash(skill.files)
        assert vendor.load(tool, skill.name) == tool.vendor_skill(skill.name)
