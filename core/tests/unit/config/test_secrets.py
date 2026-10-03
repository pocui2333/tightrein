import subprocess

import pytest

from tightrein.config.secrets import Keychain, Secret, SecretNotFound, SecretUnavailable

PASSWORD = "Pa55-w0rd!"


class FakeSecurity:
    def __init__(self, returncode=0, stdout=PASSWORD + "\n", stderr="", error=None):
        self.result = (returncode, stdout, stderr)
        self.error = error
        self.calls = []

    def __call__(self, args, timeout_seconds):
        self.calls.append(list(args))
        if self.error is not None:
            raise self.error
        returncode, stdout, stderr = self.result
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)


@pytest.fixture
def registered():
    return []


def test_reads_the_password_by_item_name(registered):
    security = FakeSecurity()
    secret = Keychain(registered.append, security).read("tightrein.demo.company")
    assert secret == Secret("tightrein.demo.company", PASSWORD)
    assert security.calls == [["security", "find-generic-password", "-s", "tightrein.demo.company", "-w"]]
    assert registered == [PASSWORD]


def test_the_value_does_not_appear_in_repr_or_str(registered):
    secret = Keychain(registered.append, FakeSecurity()).read("tightrein.demo.company")
    assert PASSWORD not in repr(secret)
    assert PASSWORD not in str(secret)
    assert str(secret) == "<钥匙串条目 tightrein.demo.company>"


def test_only_the_trailing_newline_is_removed(registered):
    secret = Keychain(registered.append, FakeSecurity(stdout=" a b \n")).read("item")
    assert secret.value == " a b "


def test_each_item_is_read_once(registered):
    security = FakeSecurity()
    keychain = Keychain(registered.append, security)
    assert keychain.read("item") is keychain.read("item")
    assert len(security.calls) == 1
    assert registered == [PASSWORD]


def test_missing_item(registered):
    security = FakeSecurity(returncode=44, stdout="", stderr="security: The specified item could not be found.\n")
    with pytest.raises(SecretNotFound) as caught:
        Keychain(registered.append, security).read("tightrein.demo.admin")
    assert caught.value.item == "tightrein.demo.admin"
    assert str(caught.value) == "钥匙串条目 tightrein.demo.admin：条目不存在"
    assert registered == []


def test_other_failures_carry_the_exit_code_and_error_output(registered):
    security = FakeSecurity(returncode=51, stdout="", stderr="line one\nUser interaction is not allowed.\n")
    with pytest.raises(SecretUnavailable) as caught:
        Keychain(registered.append, security).read("item")
    assert caught.value.reason == "security 退出码 51：User interaction is not allowed."


@pytest.mark.parametrize("error, reason", [
    (FileNotFoundError("security"), "无法执行 security：FileNotFoundError"),
    (subprocess.TimeoutExpired(["security"], 30), "无法执行 security：TimeoutExpired"),
])
def test_command_errors(registered, error, reason):
    with pytest.raises(SecretUnavailable) as caught:
        Keychain(registered.append, FakeSecurity(error=error)).read("item")
    assert caught.value.reason == reason


def test_empty_password_and_empty_item_name(registered):
    with pytest.raises(SecretUnavailable, match="密码为空"):
        Keychain(registered.append, FakeSecurity(stdout="\n")).read("item")
    with pytest.raises(ValueError):
        Keychain(registered.append, FakeSecurity()).read("")
    assert registered == []


ATTRIBUTES = '''keychain: "/Users/me/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    0x00000007 <blob>="tightrein.demo.company"
    "acct"<blob>="test-company"
    "svce"<blob>="tightrein.demo.company"
'''


def test_reads_the_account_name_from_the_attributes(registered):
    security = FakeSecurity(stdout=ATTRIBUTES)
    keychain = Keychain(registered.append, security)
    assert keychain.read_account("tightrein.demo.company") == "test-company"
    assert keychain.read_account("tightrein.demo.company") == "test-company"
    assert security.calls == [["security", "find-generic-password", "-s", "tightrein.demo.company"]]
    assert registered == []


def test_hexadecimal_account_names_are_decoded(registered):
    stdout = '    "acct"<blob>=0xE6B58BE8AF95  "\\346\\265\\213\\350\\257\\225"\n'
    assert Keychain(registered.append, FakeSecurity(stdout=stdout)).read_account("item") == "测试"


def test_missing_account_and_missing_item(registered):
    with pytest.raises(SecretUnavailable, match="acct"):
        Keychain(registered.append, FakeSecurity(stdout='    "svce"<blob>="item"\n')).read_account("item")
    with pytest.raises(SecretNotFound):
        Keychain(registered.append, FakeSecurity(returncode=44, stdout="")).read_account("item")
