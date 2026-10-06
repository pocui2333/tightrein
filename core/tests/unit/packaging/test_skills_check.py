from packaging_world import skill_text

from tightrein.cli.main import build_parser
from tightrein.config import project
from tightrein.packaging import skills_check, third_party
from tightrein.store.files.layout import ToolLayout

COMMANDS = skills_check.command_tree(build_parser())
MAX_LINES = int(project.core_config().get("packaging.skillBodyMaxLines"))
TOC_LINES = int(project.core_config().get("packaging.referenceTocLines"))


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def found(root, max_lines=MAX_LINES, skip=()):
    return sorted((issue.skill, issue.rule) for issue in skills_check.check(root, COMMANDS, max_lines=max_lines,
                                                                            toc_lines=TOC_LINES, skip=skip))


def test_every_skill_in_the_repository_passes():
    tool = ToolLayout()
    locked = {skill.name for skill in third_party.read_lock(tool.third_party_lock())}
    assert skills_check.check(tool.skills_dir(), COMMANDS, max_lines=MAX_LINES, toc_lines=TOC_LINES,
                              skip=locked) == []


def test_the_command_tree_lists_subcommands():
    assert COMMANDS["status"] is None
    assert COMMANDS["approve"] is None and COMMANDS["new"] is None
    assert {"install", "uninstall", "third-party", "skills"} <= COMMANDS["admin"]
    assert {"init", "probe", "config", "schedule"} <= COMMANDS["project"]
    assert "approve" not in COMMANDS["issue"] and "retriage" in COMMANDS["problem"]


def test_violations_are_reported_by_rule(tmp_path):
    write(tmp_path, "extra/SKILL.md", "---\nname: extra\ndescription: 多一个字段\nlicense: MIT\n---\n\n正文\n")
    write(tmp_path, "renamed/SKILL.md", skill_text("other"))
    write(tmp_path, "empty/SKILL.md", "---\nname: empty\ndescription: ''\n---\n")
    write(tmp_path, "nofront/SKILL.md", "# 没有 frontmatter\n")
    write(tmp_path, "long/SKILL.md", skill_text("long", "行\n" * 20))
    write(tmp_path, "commands/SKILL.md", skill_text("commands", "`tightrein improve propose`、`tightrein issue burn 7`\n"
                                                    "、`tightrein continue 7` 与 `tightrein approve 7`\n"))
    write(tmp_path, "refs/SKILL.md", skill_text("refs", "见 `references/a.md` 与 `references/missing.md`。\n"))
    write(tmp_path, "refs/references/a.md", "再看 [b](b.md)。\n")
    write(tmp_path, "refs/references/b.md", "执行 `tightrein nothing`。\n")
    write(tmp_path, "refs/references/roles/r.md", "由核心加载，不需要被 SKILL.md 引用\n")
    write(tmp_path, "toc/SKILL.md", skill_text("toc", "见 `references/long.md` 与 `references/listed.md`。\n"))
    write(tmp_path, "toc/references/long.md", "行\n" * (TOC_LINES + 1))
    write(tmp_path, "toc/references/listed.md", "## 目录\n" + "行\n" * TOC_LINES)
    write(tmp_path, "notaskill/README.md", "没有 SKILL.md 的目录不是 skill\n")
    assert found(tmp_path, max_lines=10) == [
        ("commands", "command"), ("commands", "command"),
        ("empty", "description"),
        ("extra", "frontmatter"),
        ("long", "length"),
        ("nofront", "frontmatter"),
        ("refs", "command"), ("refs", "reference"), ("refs", "reference-depth"), ("refs", "reference-unlisted"),
        ("renamed", "name"),
        ("toc", "reference-toc"),
    ]


def test_skipped_skills_are_not_checked(tmp_path):
    write(tmp_path, "differential-review/SKILL.md", "---\nname: differential-review\nallowed-tools: Read\n---\n")
    assert found(tmp_path) == [("differential-review", "frontmatter")]
    assert found(tmp_path, skip={"differential-review"}) == []
