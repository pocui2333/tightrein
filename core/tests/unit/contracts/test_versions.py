import pytest

from tightrein.contracts import validate, versions
from tightrein.contracts.versions import VersionError, VersionRegistry


@pytest.fixture
def registry():
    registry = VersionRegistry()
    registry.declare("doc.schema.json", 3)

    @registry.upgrader("doc.schema.json", 1)
    def rename_title(document):
        document["name"] = document.pop("title")
        return document

    @registry.upgrader("doc.schema.json", 2)
    def add_tags(document):
        document["tags"] = []
        return document

    return registry


def test_upgrade_applies_each_step_in_order(registry):
    assert registry.upgrade("doc.schema.json", {"title": "a"}, 1) == {"name": "a", "tags": []}
    assert registry.upgrade("doc.schema.json", {"name": "a"}, 2) == {"name": "a", "tags": []}


def test_upgrade_does_not_modify_the_input(registry):
    document = {"title": "a"}
    registry.upgrade("doc.schema.json", document, 1)
    assert document == {"title": "a"}


def test_current_version_is_returned_as_a_copy(registry):
    document = {"name": "a", "tags": ["x"]}
    result = registry.upgrade("doc.schema.json", document, 3)
    assert result == document
    assert result is not document


@pytest.mark.parametrize("from_version", [0, 4])
def test_version_out_of_range(registry, from_version):
    with pytest.raises(VersionError, match="没有第"):
        registry.upgrade("doc.schema.json", {}, from_version)


def test_missing_step_is_reported():
    registry = VersionRegistry()
    registry.declare("doc.schema.json", 3)
    registry.upgrader("doc.schema.json", 2)(lambda document: document)
    assert registry.missing_steps() == [("doc.schema.json", 1)]
    with pytest.raises(VersionError, match="从第 1 版到第 2 版"):
        registry.upgrade("doc.schema.json", {}, 1)


def test_unknown_schema():
    with pytest.raises(VersionError, match="未登记"):
        VersionRegistry().current("doc.schema.json")


def test_declare_rejects_duplicates_and_zero():
    registry = VersionRegistry()
    registry.declare("doc.schema.json", 1)
    with pytest.raises(ValueError, match="重复"):
        registry.declare("doc.schema.json", 2)
    with pytest.raises(ValueError, match="从 1 开始"):
        registry.declare("other.schema.json", 0)


def test_upgrader_must_lead_to_an_existing_version(registry):
    with pytest.raises(VersionError, match="不能登记"):
        registry.upgrader("doc.schema.json", 3)
    with pytest.raises(VersionError, match="已登记"):
        registry.upgrader("doc.schema.json", 1)


def test_every_schema_file_has_a_version():
    assert versions.REGISTRY.names() == validate.names()


def test_every_version_step_has_an_upgrader():
    assert versions.REGISTRY.missing_steps() == []


def test_module_functions_use_the_registry():
    assert versions.current("common.schema.json") == 1
    assert versions.upgrade("common.schema.json", {"a": 1}, 1) == {"a": 1}


def test_version_one_triage_and_issue_outputs_lose_the_removed_prioritize_label():
    triage = versions.REGISTRY.upgrade("handoff/outputs/triage.schema.json",
                                       {"labels": ["prioritize", "discuss-with-author"], "worth": None}, 1)
    assert triage["labels"] == ["discuss-with-author"]
    issue = versions.REGISTRY.upgrade("handoff/outputs/issue.schema.json", {"labels": ["prioritize"]}, 1)
    assert issue["labels"] == []
