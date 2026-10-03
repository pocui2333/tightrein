import json

import pytest
from jsonschema import Draft202012Validator

from tightrein.contracts import validate


@pytest.mark.parametrize("name", validate.names())
def test_every_schema_inlines_to_a_standalone_document(name):
    inlined = validate.inline(name)
    assert "$ref" not in json.dumps(inlined)
    assert inlined["$schema"] == validate.DIALECT
    Draft202012Validator.check_schema(inlined)


def test_every_schema_has_a_title():
    for name in validate.names():
        assert validate.schema(name)["title"], name
