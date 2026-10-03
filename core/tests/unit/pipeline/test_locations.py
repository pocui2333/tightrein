from tightrein.pipeline.common import locations


def snapshot(tmp_path):
    for path in ("processors/rule_extractor.py", "storage/exporter.py", "a/util.py", "b/util.py"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("x\n" * 200, encoding="utf-8")
    return tmp_path


def test_bare_file_names_are_completed_in_fields_and_text(tmp_path):
    root = snapshot(tmp_path)
    output = {"facts": [{"location": "rule_extractor.py:162-166",
                         "observation": "与 `rule_extractor.py:177-179` 一致，见 storage/exporter.py:85"}],
              "rootCauses": [{"file": "exporter.py", "line": 105, "symbol": None}]}
    done = locations.complete(output, root)
    fact = done.value["facts"][0]
    assert fact["location"] == "processors/rule_extractor.py:162-166"
    assert "`processors/rule_extractor.py:177-179`" in fact["observation"]
    assert "storage/exporter.py:85" in fact["observation"]
    assert done.value["rootCauses"][0]["file"] == "storage/exporter.py"
    assert done.problems() == []


def test_ambiguous_missing_or_out_of_range_references_are_reported(tmp_path):
    root = snapshot(tmp_path)
    done = locations.complete({"facts": [{"location": "util.py:3",
                                          "observation": "见 util.py:3、gone.py:1、storage/exporter.py:999、v1.2:3"}]},
                              root)
    assert done.value["facts"][0]["location"] == "util.py:3"
    assert [item.split("`")[1] for item in done.problems()] == ["util.py:3", "gone.py:1", "storage/exporter.py:999"]


def test_only_the_given_keys_are_touched(tmp_path):
    root = snapshot(tmp_path)
    done = locations.complete({"claim": {"statement": "exporter.py:1"}, "evidence": {"x": "exporter.py:1"}}, root,
                              keys=("evidence",))
    assert done.value == {"claim": {"statement": "exporter.py:1"}, "evidence": {"x": "storage/exporter.py:1"}}
