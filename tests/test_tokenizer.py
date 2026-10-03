"""
文件名: tests/test_tokenizer.py
摘要: 分词与 BOOLEAN MODE 清洗的单元测试（纯逻辑，无 DB / 无网络）。
"""

from __future__ import annotations

from melquiades.tokenizer import (
    clean_term,
    escape_like,
    has_cjk,
    split_clues,
    split_keywords,
    tokenize,
    tokenize_query,
)


def test_has_cjk():
    assert has_cjk("中文关键词")
    assert has_cjk("混合 abc 中文")
    assert not has_cjk("plain ascii only")


def test_tokenize_empty_returns_empty():
    assert tokenize("") == ""
    assert tokenize("   \t ") == ""


def test_tokenize_non_cjk_normalizes_whitespace():
    assert tokenize("  a\t b\nc ") == "a b c"


def test_clean_term_strips_boolean_operators():
    # 漂移点②：算子换成空格、两端非词字符剥掉。
    assert clean_term("+词*") == "词"
    assert clean_term("《书名》") == "书名"
    # 算子换**空格**而非删除 ⇒ `a+b` 拆成两个词，不粘成 `ab`。
    assert clean_term("a+b") == "a b"


def test_clean_term_pure_operators_is_empty():
    assert clean_term("%%") == ""
    assert clean_term('""') == ""


def test_tokenize_query_has_no_and_operator():
    # 漂移点①：不加 `+`（AND ⇒ OR）；每个词以 `*` 收尾。
    q = tokenize_query("激光 通信")
    assert "+" not in q
    assert "激光*" in q
    assert q.endswith("*")


def test_tokenize_query_strips_pure_operator_clue():
    # `%%` 这类线索清洗后为空，必须被丢弃（否则整条 MATCH 语法报错）。
    q = tokenize_query("正常 %% 词")
    assert "%%" not in q
    assert "正常*" in q


def test_split_clues_prefers_whitespace_and_dedups():
    assert split_clues("激光 地月 通信") == ["激光", "地月", "通信"]
    assert split_clues("a a b") == ["a", "b"]


def test_split_clues_empty():
    assert split_clues("") == []
    assert split_clues("   ") == []


def test_split_keywords_drops_isolated_punctuation():
    ks = split_keywords("a，b")
    assert "，" not in ks
    assert "a" in ks and "b" in ks


def test_escape_like_escapes_wildcards():
    assert escape_like("50%") == "50\\%"
    assert escape_like("a_b") == "a\\_b"
    assert escape_like("c\\d") == "c\\\\d"
    assert escape_like("plain") == "plain"
