import json

import pytest

from tightrein.domain import enums
from tightrein.domain.enums import (
    IssueStatus,
    KnowledgeType,
    LabeledEnum,
    ProblemStatus,
    RunStage,
    Severity,
    Stage,
    Verdict,
)


def test_value_is_plain_string_and_label_is_chinese():
    assert ProblemStatus.NEW == "new"
    assert ProblemStatus.NEW.value == "new"
    assert ProblemStatus.NEW.label == "新发现"


def test_enum_serializes_as_its_value():
    assert json.dumps({"s": IssueStatus.PENDING_MERGE}) == '{"s": "pending-merge"}'


def test_lookup_by_value():
    assert Verdict("insufficient") is Verdict.INSUFFICIENT


def test_values_use_lowercase_and_hyphen_except_severity():
    for enum_cls in enums.ALL_ENUMS:
        for member in enum_cls:
            if enum_cls is Severity:
                assert member.value in {"P0", "P1", "P2", "P3"}
                continue
            assert member.value == member.value.lower(), member
            assert "_" not in member.value, member


def test_run_stage_is_stage_plus_loop():
    assert {m.value for m in RunStage} == {m.value for m in Stage} | {"loop"}


def test_knowledge_type_prefix():
    assert KnowledgeType.DEFECT_PATTERN.prefix == "DP"
    assert KnowledgeType.from_prefix("FL") is KnowledgeType.FIX_LESSON
    with pytest.raises(KeyError):
        KnowledgeType.from_prefix("XX")


def test_every_member_has_label():
    for enum_cls in enums.ALL_ENUMS:
        assert issubclass(enum_cls, LabeledEnum)
        for member in enum_cls:
            assert member.label
