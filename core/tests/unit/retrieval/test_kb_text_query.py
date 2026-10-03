import sqlite3

import pytest

from tightrein.retrieval.errors import InvalidQuery
from tightrein.config import layers
from tightrein.retrieval.query import match_expression, rank_expression, terms
from tightrein.retrieval.text import estimate_tokens, has_token, split_cjk

DOCUMENTS = {
    "cn": "数据权限校验缺失时接口返回全部公司的数据",
    "jp": "ユーザー権限のチェックが抜けている",
    "route": "POST /api/Material/Query 缺少公司过滤",
    "ident": "order_id 为空时 OrderService.Page 抛出异常",
    "apart": "order 页面与 id 字段分开出现",
    "reverse": "限权两个字顺序相反",
}


@pytest.fixture
def fts():
    conn = sqlite3.connect(":memory:")
    conn.execute('CREATE VIRTUAL TABLE docs USING fts5(id UNINDEXED, body, tokenize = "unicode61 remove_diacritics 2")')
    conn.executemany("INSERT INTO docs (id, body) VALUES (?, ?)",
                     [(key, split_cjk(text)) for key, text in DOCUMENTS.items()])
    yield conn
    conn.close()


def matching(conn, query):
    rows = conn.execute("SELECT id FROM docs WHERE docs MATCH ? ORDER BY id", (match_expression(query),))
    return [row[0] for row in rows]


def test_cjk_characters_become_single_tokens():
    assert split_cjk("数据权限校验") == "数 据 权 限 校 验"
    assert split_cjk("ユーザー権限check_id") == "ユ ー ザ ー 権 限 check_id"
    assert split_cjk("  Page\t=  1 ") == "Page = 1"
    assert split_cjk("한국어") == "한 국 어"


def test_a_two_character_word_hits_a_long_sentence_but_not_reversed_order(fts):
    assert matching(fts, "权限") == ["cn"]
    assert matching(fts, "権限") == ["jp"]


def test_routes_and_identifiers_match_as_phrases(fts):
    assert matching(fts, "/api/Material/Query") == ["route"]
    assert matching(fts, "order_id") == ["ident"]
    assert matching(fts, "OrderService.Page") == ["ident"]


def test_terms_are_joined_with_or_and_quotes_keep_a_term_whole(fts):
    assert terms('权限 "Material Query"  order_id') == ["权限", "Material Query", "order_id"]
    assert match_expression('权限 "Material Query"') == '"权 限" OR "Material Query"'
    assert matching(fts, "权限 order_id") == ["cn", "ident"]


@pytest.mark.parametrize("query", ['a"b', "Page*", "col:value", "order AND id", "(order)", "^start", "NEAR(a b)",
                                   'unclosed "quote'])
def test_syntax_characters_are_not_operators(fts, query):
    matching(fts, query)
    assert all(part.startswith('"') and part.endswith('"') for part in match_expression(query).split(" OR "))


def test_doubled_quotes_inside_a_phrase():
    assert match_expression('a"b') == '"a""b"'


@pytest.mark.parametrize("query", ["", "   ", "!!! ... ---", '"" ()'])
def test_queries_without_text_are_invalid(query):
    with pytest.raises(InvalidQuery):
        match_expression(query)


def test_token_detection_and_estimate():
    assert has_token("_") is False and has_token("a") is True and has_token("权") is True
    assert estimate_tokens("权限校验") == 4
    assert estimate_tokens("abcdefgh ij") == 3
    assert estimate_tokens("权限 abcde") == 4


def test_rank_expression_carries_the_documented_weights():
    weights = layers.core_value("runtime.retrieval.columnWeights")
    assert rank_expression(weights) == "bm25(knowledge_fts, 0.0, 10.0, 5.0, 5.0, 1.0)"
