import json
import re

import pytest

from tightrein.cli.text import DIRECTORY, LANGUAGES, TextMissing, has, text


def test_values_are_filled_by_name():
    assert text("zh", "status.waiting", count=2) == "等你处理 2"
    assert text("en", "status.waiting", count=2) == "Needs you 2"
    assert text("zh", "points.implement.code") == "编码"  # 名本身带点：只在第一个点处分开
    assert text("en", "points.collect.platform_errors") == "platform_errors"


def test_missing_keys_and_values_are_errors():
    with pytest.raises(TextMissing):
        text("zh", "status.no_such_key")
    with pytest.raises(TextMissing):
        text("zh", "status.waiting")
    with pytest.raises(ValueError):
        text("fr", "status.waiting", count=1)
    assert has("zh", "status.waiting") and not has("zh", "status.nope")


def test_both_tables_have_the_same_keys_and_placeholders():
    tables = {language: json.loads((DIRECTORY / f"{language}.json").read_text(encoding="utf-8"))
              for language in LANGUAGES}
    zh, en = tables["zh"], tables["en"]
    assert set(zh) == set(en)
    assert "help" in zh
    for section in zh:
        assert set(zh[section]) == set(en[section]), section
        if section == "common":
            continue  # common.point：中文用阶段与小步骤名拼，英文直接用控制键
        for key in zh[section]:
            assert set(re.findall(r"\{(\w+)\}", zh[section][key])) == set(re.findall(r"\{(\w+)\}", en[section][key]))
