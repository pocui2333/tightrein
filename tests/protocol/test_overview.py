import json
from pathlib import Path

from tightrein.protocol import overview

REPO_DEFAULTS = Path(__file__).resolve().parents[2] / "settings" / "defaults.json"


def test_the_readme_is_generated_from_the_current_defaults() -> None:
    # README 不手写：缺省值改了而没有重新生成时这里会失败
    defaults = json.loads(REPO_DEFAULTS.read_text(encoding="utf-8"))
    assert overview.README.read_text(encoding="utf-8") == overview.render(defaults)


def test_every_listed_key_exists_in_the_defaults() -> None:
    defaults = json.loads(REPO_DEFAULTS.read_text(encoding="utf-8"))
    rendered = overview.render(defaults)
    for entry in overview.ENTRIES:
        assert f"`{entry.document}`" in rendered
    assert "| `timeout` | 10m |" in rendered and "`stale`：90s" in rendered
