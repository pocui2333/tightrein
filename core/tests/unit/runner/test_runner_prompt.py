from runner_samples import task

from tightrein.domain import language
from tightrein.runner.prompt import build_prompt
from tightrein.store.files.layout import ToolLayout


def test_the_output_language_section_comes_before_the_output_section(tmp_path):
    prompt = build_prompt(task(), ToolLayout(tmp_path), language="zh")
    assert "# 输出语言" in prompt and "都用简体中文书写" in prompt
    assert prompt.index("# 输出语言") < prompt.index("# 输出\n")


def test_no_language_section_without_a_language(tmp_path):
    assert "# 输出语言" not in build_prompt(task(), ToolLayout(tmp_path))


def test_fixed_sections_come_before_task_instructions(tmp_path):
    prompt = build_prompt(task(), ToolLayout(tmp_path), language="zh")
    assert prompt.index("# 输出\n") < prompt.index("# 任务\n")


def test_language_names_and_fixed_texts_fall_back_to_english():
    assert (language.name("ja"), language.name("zh-CN"), language.name("fr")) == ("日本語", "简体中文", "fr")
    texts = {"zh": "问题", "en": "Problem"}
    assert (language.pick(texts, "zh-TW"), language.pick(texts, "fr")) == ("问题", "Problem")
