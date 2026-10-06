"""分层代码摘要：核心位置截取原文，相关位置只取定义行，同一位置只记一次，原文来自 worktree。"""

from tightrein.pipeline.fix.steps import brief

SOURCE = "\n".join([
    "import os",
    "",
    "def check_login(site):",
    "    profile = os.path.join('p', site)",
    "    return launch(profile, headless=True)",
    "",
    "class Runner:",
    "    def run(self):",
    "        return check_login('zhihu')",
]) + "\n"


def test_core_excerpts_related_signatures_and_files(tmp_path):
    (tmp_path / "app.py").write_text(SOURCE, encoding="utf-8")
    scouting = {"existing": [{"location": "app.py:5", "description": "以无头模式启动"}],
                "problems": [{"location": "app.py:5", "description": "重复"}],
                "reusable": [{"location": "app.py:9", "description": "调用方"}],
                "linkage": [{"location": "missing.py:3", "description": "不存在的文件"}],
                "designIssue": None}
    found = brief.build(scouting, tmp_path)
    assert [item["location"] for item in found["core"]] == ["app.py:5"]
    assert "headless=True" in found["core"][0]["excerpt"] and "    3  def check_login(site):" in found["core"][0]["excerpt"]
    assert found["related"] == [{"location": "app.py:9", "description": "调用方", "signature": "8: def run(self):"}]
    assert found["files"] == ["app.py", "missing.py"]
    text = brief.render(found)
    assert text.startswith("## 代码摘要") and "<code_brief>" in text and "app.py:9" in text
    brief.save(tmp_path, found)
    assert brief.load(tmp_path) == found


def test_no_brief_renders_nothing(tmp_path):
    assert brief.render(None) == "" and brief.render(brief.build({}, tmp_path)) == ""
    assert brief.load(tmp_path) is None
