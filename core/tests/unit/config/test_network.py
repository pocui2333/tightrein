import urllib.request
from pathlib import Path

from tightrein.config import network
from tightrein.config.user import UserConfig

PROXY = "http://127.0.0.1:8118"
GITHUB = ("github.com", "api.github.com", "codeload.github.com", "objects.githubusercontent.com",
          "raw.githubusercontent.com")


def configured(proxy=PROXY, no_proxy=GITHUB):
    return UserConfig(Path("config.yaml"), network_proxy=proxy, no_proxy=no_proxy)


def test_the_proxy_and_no_proxy_are_set_in_both_cases():
    found = network.environ({"PATH": "/bin", "no_proxy": "old"}, configured())
    assert {found[name] for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")} == {PROXY}
    expected = ",".join([*GITHUB, "localhost", "127.0.0.1", "::1"])
    assert found["no_proxy"] == found["NO_PROXY"] == expected
    assert found["PATH"] == "/bin"


def test_without_a_proxy_the_environment_is_unchanged():
    base = {"PATH": "/bin", "https_proxy": "http://other:1", "no_proxy": "x"}
    assert network.environ(base, UserConfig(Path("config.yaml"))) == base


def test_the_proxy_table_reads_the_given_mapping_with_lowercase_first():
    table = network.proxies({"HTTPS_PROXY": "http://upper:1", "https_proxy": "http://lower:1", "NO_PROXY": "a.test",
                             "PATH": "/bin", "ftp_proxy": ""})
    assert table == {"https": "http://lower:1", "no": "a.test"}
    environment = network.environ({}, configured())
    table = network.proxies(environment)
    assert table["http"] == table["https"] == PROXY
    assert urllib.request.proxy_bypass_environment("api.github.com", table)
    assert urllib.request.proxy_bypass_environment("localhost", table)
    assert not urllib.request.proxy_bypass_environment("staging.example.test", table)


def test_proxy_passwords_are_found_and_masked():
    assert network.proxy_password(configured()) is None
    assert network.proxy_password(configured("http://me:s3cret@proxy.test:8118")) == "s3cret"
    assert network.masked_proxy("http://me:s3cret@proxy.test:8118") == "http://me:[已脱敏]@proxy.test:8118"
    assert network.masked_proxy(PROXY) == PROXY


def test_routes_and_the_other_route():
    direct = network.environ({}, configured())
    assert network.route(direct, "github.com") == network.DIRECT
    proxied = network.rerouted(direct, "github.com")
    assert network.route(proxied, "github.com") == network.PROXIED
    assert proxied["no_proxy"] == proxied["NO_PROXY"] == "objects.githubusercontent.com,raw.githubusercontent.com,localhost,127.0.0.1,::1"
    assert proxied["https_proxy"] == PROXY
    through = network.environ({}, configured(no_proxy=()))
    assert network.route(through, "github.com") == network.PROXIED
    back = network.rerouted(through, "github.com")
    assert network.route(back, "github.com") == network.DIRECT and back["NO_PROXY"].endswith(",github.com")
    assert network.route({"HTTPS_PROXY": PROXY, "no_proxy": ".github.com"}, "github.com") == network.DIRECT
    assert network.route({"HTTPS_PROXY": PROXY, "no_proxy": "*"}, "github.com") == network.DIRECT
    assert network.route({"HTTPS_PROXY": PROXY, "no_proxy": "hub.com"}, "github.com") == network.PROXIED
    assert network.rerouted({"PATH": "/bin"}, "github.com") is None


def test_network_failures_are_recognised_by_pattern():
    patterns = network_patterns()
    for text in ("fatal: unable to access 'https://github.com/o/r/': Could not resolve host: github.com",
                 "ssh: connect to host github.com port 22: Operation timed out",
                 "fatal: unable to access 'https://github.com/o/r/': LibreSSL SSL_connect: SSL_ERROR_SYSCALL",
                 "error connecting to api.github.com", "net/http: TLS handshake timeout",
                 "read: connection reset by peer"):
        assert network.is_network_failure(text, patterns), text
    for text in ("remote: Invalid username or password.", "! [rejected]        main -> main (non-fast-forward)",
                 "git@github.com: Permission denied (publickey).", "HTTP 404: Not Found"):
        assert not network.is_network_failure(text, patterns), text


def network_patterns():
    from tightrein.config import layers

    return layers.core_value("runtime.network.errorPatterns")
