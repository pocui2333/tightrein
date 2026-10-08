import tomllib

from schemathesis.config import SanitizationConfig

from tightrein.collect.api_fuzz.schemathesis import config_writer


def test_config_file_has_no_token_and_keeps_default_sanitization(tmp_path):
    path = config_writer.write(tmp_path / "raw" / "schemathesis.toml", 2, ["X-Company-Key", "authorization"],
                               ("Authorization", "Bearer "))
    text = path.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert data["workers"] == 2
    assert data["headers"] == {"Authorization": "Bearer ${TIGHTREIN_TOKEN}"}
    sanitization = data["output"]["sanitization"]
    keys = sanitization["keys-to-sanitize"]
    assert sanitization["enabled"] is True
    # 给出 keys-to-sanitize 会替换缺省清单：缺省清单要原样写进去，再追加项目的与凭证请求头名
    assert keys[:len(SanitizationConfig().keys_to_sanitize)] == list(SanitizationConfig().keys_to_sanitize)
    assert "x-company-key" in keys and keys.count("authorization") == 1


def test_static_header_and_anonymous_runs():
    static = tomllib.loads(config_writer.render(1, [], ("X-Access-Code", "")))
    assert static["headers"] == {"X-Access-Code": "${TIGHTREIN_TOKEN}"}
    assert "x-access-code" in static["output"]["sanitization"]["keys-to-sanitize"]
    anonymous = tomllib.loads(config_writer.render(1, [], None))
    assert "headers" not in anonymous
