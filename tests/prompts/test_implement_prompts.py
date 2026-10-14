"""方案提示中哪些不算要拍板的点(只在提示文字里，用断言守住)：写进 userDecisions 的点全自动时也会停在关卡。"""

from tightrein.prompts.build import PROMPTS_DIR


def text(point: str) -> str:
    return (PROMPTS_DIR / f"{point}.md").read_text(encoding="utf-8")


def test_code_organisation_is_the_planners_call_not_a_decision_point():
    """新建文件或模块、函数放哪、拆分与命名由方案自己定，不问用户「是否同意新建模块」。"""
    found = text("implement.design")
    assert "新模块与新目录都不由你决定" not in found
    assert "代码怎么组织由你决定" in found and "不交用户" in found
    assert "「代码怎么组织」" in found and "函数放进哪个文件" in found and "拆不拆现有模块" in found
    assert "新建文件标 `isNew` 并写明为什么没有合适的现有落点" in found


def test_the_other_non_decisions_stay_and_real_ones_still_go_to_the_user():
    found = text("implement.design")
    assert "「要不要顺带做 Issue 范围以外的事」" in found and "「方案算不算满足某条验收标准」" in found
    assert "「要不要等别的 Issue 合入再做」也不写进 `userDecisions`" in found
    assert "新增依赖、删除文件不由你决定，写进对应字段(`newDependencies`、`deletions`)交用户确认" in found
    assert "`protectedTouches`" in found and "把矛盾写进 `userDecisions`" in found
