from tightrein.collect.static import context
from tightrein.collect.static.context import Limits

LIMITS = Limits(context_lines=2, function_lines=40, changes_chars=10_000, knowledge_entries=5, knowledge_tokens=500)
PYTHON = ["import os", "", "class Service:", "    def run(self, page):", "        size = 20",
          "        rows = query(",
          "            page * size)", "        return rows", "", "    def other(self):", "        pass"]
CSHARP = ["using System;", "public class OrderService", "{", "    public int Page(int page)", "    {",
          "        if (page < 0) {", "            throw new Exception();", "        }", "        return page;", "    }",
          "}"]


def test_changed_lines_are_counted_on_the_new_side():
    diff = ("diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,3 +1,4 @@\n line1\n-old\n"
            "+new\n+added\n line3\n@@ -10 +11,2 @@\n+tail\n context\n\\ No newline at end of file\n")
    assert context.changed_lines(diff) == {"src/a.py": [2, 3, 11]}


def test_the_enclosing_function_by_indentation_and_by_braces():
    assert context.enclosing(PYTHON, 6, LIMITS) == (4, 8)
    assert context.enclosing(CSHARP, 7, LIMITS) == (4, 10)


def test_without_a_definition_or_with_a_long_function_only_nearby_lines_are_taken():
    assert context.enclosing(["a", "b", "c", "d", "e", "f"], 4, LIMITS) == (2, 6)
    assert context.enclosing(PYTHON, 5, Limits(2, 3, 10_000, 5, 500)) == (3, 7)


def test_changes_text_has_the_diff_and_the_whole_function(static_runtime, repos):
    runtime = static_runtime()
    base = repos.git(repos.repo, "rev-parse", "HEAD").strip()
    text = (repos.repo / "src" / "orders.py").read_text(encoding="utf-8").replace("size = 20", "size = 50")
    head = repos.push("feat: 改页大小", {"src/orders.py": text})
    rendered = context.changes_text(runtime.git, base, head, ["src/orders.py"], repos.repo, LIMITS)
    assert "-    size = 20" in rendered and "+    size = 50" in rendered
    assert "    1  def list_orders(page):" in rendered and "    4      return rows" in rendered
    assert "delete_order" not in rendered.split("## 改动所在的函数")[1]
    assert context.changes_text(runtime.git, base, head, [], repos.repo, LIMITS) == "(无)"
    cut = context.changes_text(runtime.git, base, head, ["src/orders.py"], repos.repo, Limits(2, 40, 50, 5, 500))
    assert cut.endswith("其余改动请按 diff 自己读)") and len(cut) < 100


def test_the_verification_excerpt_is_the_function_of_the_claim(tmp_path):
    (tmp_path / "a.py").write_text("\n".join(PYTHON) + "\n", encoding="utf-8")
    excerpt = context.excerpt(tmp_path, "a.py", 5, LIMITS)
    assert "    4      def run(self, page):" in excerpt and "other" not in excerpt
    assert context.excerpt(tmp_path, "missing.py", 1, LIMITS) == "(无)"
    assert context.excerpt(tmp_path, "a.py", 99, LIMITS) == "(无)"


def test_knowledge_and_defect_patterns_come_from_the_workspace(source_runtime):
    runtime = source_runtime()
    assert context.knowledge(runtime.workspace, ["src/a.py"], LIMITS).startswith("没有与本次相关的知识条目")
    assert context.defect_patterns(runtime.workspace) == [] and context.tradeoffs(runtime.workspace) == "(无)"
