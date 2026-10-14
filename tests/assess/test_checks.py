from collections.abc import Callable
from pathlib import Path
from typing import Any

from tightrein.assess import checks
from tightrein.assess.checks import Snapshot, complete

WORDS = ["可能", "大概", "建议进一步排查"]
CLAIM = {"facts": [{"label": "出现次数", "value": 3}, {"label": "信号", "value": {}}],
         "entryPoints": ["GET /orders/{id}"]}


def run(output: dict[str, Any], repo: Path, *, light: bool = False) -> list[str]:
    return checks.check(output, CLAIM, Snapshot(repo), vague_words=WORDS, light=light)


def test_a_good_output_passes(repo: Path, good_output: Callable[..., dict[str, Any]]) -> None:
    assert run(good_output(), repo) == []


def test_bare_file_names_are_completed_in_fields_and_text(repo: Path) -> None:
    value = {"rootCauses": [{"file": "ci.yml", "line": 1}], "facts": [{"location": "./services/orders.py:3"}],
             "trigger": "见 orders.py:3 与 workflows/ci.yml:2"}
    done = complete(value, Snapshot(repo))
    assert done.value["rootCauses"][0]["file"] == ".github/workflows/ci.yml"  # 隐藏目录不会被削掉
    assert done.value["facts"][0]["location"] == "services/orders.py:3"
    assert "workflows/ci.yml" in done.value["trigger"] and ".github/workflows/ci.yml:2" in done.value["trigger"]
    assert any("orders.py:3" in problem for problem in done.problems())  # 两个 orders.py：不补，报出来


def test_ambiguous_missing_or_out_of_range_references_are_reported(repo: Path) -> None:
    snapshot = Snapshot(repo)
    assert snapshot.resolve("orders.py") is None  # 多个匹配
    assert snapshot.resolve("nothing.py") is None
    assert snapshot.problem("services/orders.py:99") == "services/orders.py 只有 8 行，引用了第 99 行"
    assert "不存在" in str(snapshot.problem("nothing.py:1"))
    done = complete({"note": "参考 README 与 version 1:2，及 services/orders.py:99"}, snapshot)
    assert done.problems() == [
        "[locations] 文字中引用的 `services/orders.py:99` 在代码中找不到唯一的文件或行号越界，写相对仓库根的完整路径"]


# Issue 0028「引用」中的原句：代理地址曾被当成代码位置 `//127.0.0.1:8118`
PROXY_SENTENCE = ("`config/config.yaml:89` 默认配置设了 ai_review.proxy: http://127.0.0.1:8118，"
                  "按默认配置运行时 agy 的 http(s) 代理会补上")


def agy_repo(tmp_path: Path) -> Path:
    """Issue 0028 所在仓库的替身：processors/ai_review.py 440 行，config/config.yaml 100 行。"""
    root = tmp_path / "agy"
    (root / "processors").mkdir(parents=True)
    (root / "processors" / "ai_review.py").write_text("x = 1\n" * 440, encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "config.yaml").write_text("key: value\n" * 100, encoding="utf-8")
    return root


def test_urls_and_host_ports_are_not_code_locations(tmp_path: Path) -> None:
    assert [match.group(0) for match in checks.locations_in(PROXY_SENTENCE)] == ["config/config.yaml:89"]
    snapshot = Snapshot(agy_repo(tmp_path))
    text = ("代理 http://127.0.0.1:8118/processors/ai_review.py:313 或 127.0.0.1:8118、localhost:8080、"
            "proxy.example.com:3128，见 https://github.com/acme/shop/blob/main/ai_review.py:7")
    done = complete({"note": text, "facts": [{"location": "processors/ai_review.py:313",
                                              "observation": PROXY_SENTENCE}]}, snapshot)
    assert done.value["note"] == text and done.problems() == []  # 网址里的路径、主机:端口都不补、不报


def test_host_port_paths_without_a_scheme_are_not_code_locations(tmp_path: Path) -> None:
    """不带协议头的「主机:端口/路径」：不能从端口后面截出 `8080/processors/ai_review.py:313` 当成位置。"""
    snapshot = Snapshot(agy_repo(tmp_path))
    text = ("打开 localhost:8080/processors/ai_review.py:313 看看，代理 127.0.0.1:8118/src/app/main.py:3、"
            "proxy.example.com:3128/processors/ai_review.py:7、//cdn.example.com:80/app.js:3")
    assert checks.locations_in(text) == []
    done = complete({"note": text}, snapshot)
    assert done.value["note"] == text and done.problems() == []
    beside = "打开 localhost:8080/api，调用点 ai_review.py:313 与 ai_review.py:313/379 两处"
    assert complete({"note": beside}, snapshot).value["note"] == (
        "打开 localhost:8080/api，调用点 processors/ai_review.py:313 与 processors/ai_review.py:313/379 两处")


def test_real_locations_next_to_urls_are_still_completed_and_checked(tmp_path: Path) -> None:
    snapshot = Snapshot(agy_repo(tmp_path))
    beside = "代理 http://127.0.0.1:8118，调用点 ai_review.py:313(http://127.0.0.1:8118)processors/ai_review.py:379"
    done = complete({"note": beside}, snapshot)
    assert done.value["note"] == ("代理 http://127.0.0.1:8118，调用点 processors/ai_review.py:313"
                                  "(http://127.0.0.1:8118)processors/ai_review.py:379")
    assert done.problems() == []
    wrong = complete({"note": "代理 http://127.0.0.1:8118 processors/ai_review.py:999"}, snapshot)
    assert len(wrong.problems()) == 1 and "`processors/ai_review.py:999`" in wrong.problems()[0]


def test_only_the_given_keys_are_touched(repo: Path) -> None:
    value = {"claim": "routes/orders.py:4", "rootCauses": [{"file": "routes/orders.py", "line": 4}],
             "facts": [{"location": "../orders.py:1"}]}
    done = complete(value, Snapshot(repo), keys=["rootCauses"])
    assert done.value["claim"] == "routes/orders.py:4" and done.value["facts"] == value["facts"]


def test_each_check_fails_on_its_own_defect(repo: Path, good_output: Callable[..., dict[str, Any]]) -> None:
    assert run(good_output(verdict="maybe"), repo)[0].startswith("[verdict]")
    assert run(good_output(verdict="insufficient", missingInfo=[]), repo)[0].startswith("[verdict]")
    assert run(good_output(verdict="conditional", trigger=None), repo)[0].startswith("[verdict]")
    bad_fact = good_output(facts=[{"location": "services/orders.py:42", "observation": "x"}])
    assert [item for item in run(bad_fact, repo) if item.startswith("[evidence-location]")]
    vague = good_output(trigger="可能在并发时出现")
    assert run(vague, repo) == ["[vague-wording] 结论含含糊措辞：trigger 含「可能」"]
    described = good_output(facts=[{"location": "services/orders.py:3", "observation": "返回前 order 都可能为空"}])
    assert run(described, repo) == []  # 代码事实描述可能的状态，不算含糊
    no_assessment = good_output(assessment=None)
    assert run(no_assessment, repo)[0].startswith("[assessment]")
    missing_file = good_output()
    missing_file["assessment"]["files"] = [{"path": "services/new.py", "isNew": False}]
    assert "services/new.py" in run(missing_file, repo)[0]
    deferred = good_output()
    deferred["assessment"]["worth"] = "defer"
    assert "reevaluateWhen" in run(deferred, repo)[0]


def test_refuted_source_by_location_or_fact_and_insufficient_needs_missing_info(
        repo: Path, good_output: Callable[..., dict[str, Any]]) -> None:
    refuted = good_output(verdict="refuted", report=None, assessment=None, rootCauses=[],
                          sourceOfPhenomenon={"location": None, "factRef": 2, "explanation": "请求本身不合法"})
    assert run(refuted, repo) == []
    both = dict(refuted, sourceOfPhenomenon={"location": "services/orders.py:3", "factRef": 1, "explanation": "x"})
    assert "恰好给出一个" in run(both, repo)[0]
    wrong_fact = dict(refuted, sourceOfPhenomenon={"location": None, "factRef": 9, "explanation": "x"})
    assert "共 2 条" in run(wrong_fact, repo)[0]
    assert "没有说明现象" in run(dict(refuted, sourceOfPhenomenon=None), repo)[0]
    insufficient = good_output(verdict="insufficient", report=None, assessment=None, counterEvidence=[],
                               missingInfo=[{"item": "业务量级", "source": "user"}])
    assert run(insufficient, repo) == []


def test_counter_check_traces_an_entry_and_the_upstream_validation(
        repo: Path, good_output: Callable[..., dict[str, Any]]) -> None:
    assert run(good_output(counterEvidence=[]), repo) == ["[counter-check] 没有反证检查，至少沿调用链追到一个入口"]
    assert run(good_output(counterEvidence=[]), repo, light=True) == []
    quoted = good_output(counterEvidence=[{"check": "c", "entry": "GET /orders/{id}", "result": "r",
                                           "upstreamValidation": {"status": "absent", "location": None}}])
    assert run(quoted, repo) == []  # 原样引用主张中的入口
    annotated = good_output(counterEvidence=[{"check": "c", "entry": "routes/orders.py:4（订单详情的路由）", "result": "r",
                                              "upstreamValidation": {"status": "absent", "location": None}}])
    assert run(annotated, repo) == []  # 位置后面带说明：只核对开头的位置
    unknown = good_output(counterEvidence=[{"check": "c", "entry": "somewhere", "result": "r",
                                            "upstreamValidation": {"status": "present", "location": None}}])
    reasons = run(unknown, repo)
    assert len(reasons) == 2 and "入口" in reasons[0] and "没有给出校验的位置" in reasons[1]


def test_values_at_expands_lists() -> None:
    value = {"a": [{"b": 1}, {"b": 2}], "c": {"d": None}}
    assert checks.values_at(value, "a[*].b") == [("a[0].b", 1), ("a[1].b", 2)]
    assert checks.values_at(value, "c.d") == [("c.d", None)]
    assert checks.values_at(value, "x.y") == []
