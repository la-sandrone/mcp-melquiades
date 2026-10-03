"""
文件名: tests/test_vocab.py
摘要: 受控词表（同义词环）的相似度与形态判据（纯逻辑）。
"""

from __future__ import annotations

from melquiades.vocab import (
    char_dice,
    contains,
    is_inverted_pair,
    is_parallel_series,
    jw,
    norm_key,
    similarity,
)


def test_jw_identical_is_one():
    assert jw("melquiades", "melquiades") == 1.0


def test_jw_related_beats_unrelated():
    assert jw("melquiades", "melquiade") > jw("melquiades", "zzzzzzzz")


def test_char_dice_known_values():
    # 文档中给出的实测值。
    assert char_dice("裁决", "裁定") == 0.5
    assert char_dice("修正", "勘误") == 0.0


def test_contains_normalized():
    assert contains("修正", "修正包")
    assert not contains("修正包", "修正")
    assert not contains("同", "同")  # 相等不算包含


def test_inverted_pair_detects_anagram():
    assert is_inverted_pair("守灯", "灯守")
    assert is_inverted_pair("上下文恢复", "恢复上下文")


def test_inverted_pair_rejects_identical():
    assert not is_inverted_pair("守灯", "守灯")
    assert not is_inverted_pair("a", "a")  # 长度 < 2


def test_parallel_series_not_synonym():
    assert is_parallel_series("附录H", "附录C")
    assert not is_parallel_series("附录H", "附录H")


def test_norm_key_folds_separators_and_case():
    assert norm_key("Mare_Cordis") == norm_key("Mare Cordis")
    assert norm_key("ABC") == norm_key("abc")


def test_similarity_dict_shape():
    d = similarity("裁决", "裁定")
    assert set(d) == {"norm_eq", "inverted", "contain", "dice", "jw"}
    assert d["dice"] == 0.5
