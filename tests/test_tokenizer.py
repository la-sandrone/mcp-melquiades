"""
文件名: tests/test_tokenizer.py
摘要: 分词与 BOOLEAN MODE 清洗的单元测试（纯逻辑，无 DB / 无网络）。
"""

from __future__ import annotations

from melquiades.config import SearchConfig
from melquiades.tokenizer import (
    build_cjk_query_v2,
    clean_term,
    escape_like,
    has_cjk,
    split_clues,
    split_keywords,
    tokenize,
    tokenize_cjk_ub_v2,
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


# ══════════════════════════════════════════════════════════════════════════
# A1/A2 回归（2026-11-07 检索同源缺陷审计）
#
# 这一节守的是**召回**，不是排序：查询侧产出的 token 必须能在索引侧找到。
# 一旦查询侧改了 token 边界（切块、删字、替换），就会出现「索引里有却查不到」，
# 而且永不报错 —— 所以只能靠这几条不变量测试兜住。
# ══════════════════════════════════════════════════════════════════════════

# 审计文档 A1 里那批「含停词字的真词」：旧实现（子串替换）把它们当场切碎。
STOPWORD_INSIDE_WORDS = (
    "地质", "地层", "过程", "过滤", "超过", "太阳", "能量", "性能", "要求", "重要",
    "真实", "真空", "更新", "最近", "说明", "学说", "对比", "对应", "到达", "归还",
    "再生", "天才", "会话", "社会", "机会", "有机", "所有", "心得",
)


def test_no_stopword_fragments_real_words():
    """A1：含停词字的真词必须完整产出自己的 bigram（旧实现 28/28 全碎）。"""
    sw = tuple(SearchConfig().stopwords)
    broken = [w for w in STOPWORD_INSIDE_WORDS
              if w not in build_cjk_query_v2(w, sw).split()]
    assert broken == [], "被切碎的真词: %r" % (broken,)


def test_query_tokens_are_subset_of_index_tokens():
    """A2：查询侧 token 必须全部存在于索引侧（memoria 那边同款不变量）。"""
    sw = tuple(SearchConfig().stopwords)
    samples = list(STOPWORD_INSIDE_WORDS) + [
        "算不算命", "等离子体生命算不算生命", "为什么地球会有磁场呢",
        "JDBC接口", "X射线等离子体", "Lyfe 四支柱是什么", "melquiades.harvest",
        "有没有 一种 可能", "地球化学", "真空层",
    ]
    for text in samples:
        q = set(build_cjk_query_v2(text, sw).split())
        idx = set(tokenize_cjk_ub_v2(text).split())
        assert q <= idx, "查询侧多出索引里不存在的 token: %r (text=%r)" % (q - idx, text)


def test_multichar_stopword_does_not_leak_boundary_bigrams():
    """多字停词仍是整词：跨它的 bigram 不许出现（旧实现的初衷，不能一起丢掉）。"""
    sw = tuple(SearchConfig().stopwords)
    tokens = build_cjk_query_v2("为什么地球会有磁场呢", sw).split()
    assert "么地" not in tokens and "为什" not in tokens
    assert "地球" in tokens


def test_single_char_query_never_empties():
    """单字查询不能归零（bigram 集为空时必须退回 unigram）。"""
    sw = tuple(SearchConfig().stopwords)
    for ch in ("鲸", "体", "冰"):
        assert build_cjk_query_v2(ch, sw).split() == [ch]


def test_all_stopword_query_falls_back_to_unigrams():
    """整句都是停词时退回 unigram，而不是原样返回（兜底不许把规则抵消掉）。"""
    sw = tuple(SearchConfig().stopwords)
    assert build_cjk_query_v2("算不算命", sw).split() == ["算", "不", "命"]


def test_stopword_list_has_no_high_frequency_builders():
    """A1 的表约束：单字停词只留几乎不构词的那几个。"""
    single = [w for w in SearchConfig().stopwords if len(w) == 1]
    assert sorted(single) == sorted(list("的了吗呢吧啊呀哦嗯"))

