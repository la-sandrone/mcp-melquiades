"""
文件名: tests/test_compaction_v2_contract.py
摘要: v1(seq)→v2(guid) 翻译逻辑中**不依赖数据库**的两条契约：
      文件名→代数、裸 uuid→44 字符 session_id。
      这两条是跨代际指针正确性的地基，上游日志命名一变最先在这里响。
"""

from __future__ import annotations

from melquiades.harvest.compaction_v2 import gens_of, session_id_44


def test_gens_of_reads_generation_from_filename():
    assert gens_of("/root/--ws--/session-x/session.v4.jsonl.zstd") == 4
    assert gens_of("/root/--ws--/session-x/session.v10.jsonl.zstd") == 10


def test_gens_of_gen0_for_bare_name():
    # 规范名 `session.jsonl.zstd` = 代 0。
    assert gens_of("/root/--ws--/session-x/session.jsonl.zstd") == 0


def test_gens_of_returns_none_when_unparseable():
    # 解析不出就返回 None（不猜成 0）—— 猜错会让跨代际 seq 撞车。
    assert gens_of("/root/some-other-file.jsonl") is None
    assert gens_of("") is None


def test_session_id_44_adds_prefix():
    assert session_id_44("abcdef01-2345-6789-abcd-ef0123456789") == \
        "session-abcdef01-2345-6789-abcd-ef0123456789"


def test_session_id_44_idempotent():
    sid = "session-abcdef01-2345-6789-abcd-ef0123456789"
    assert session_id_44(sid) == sid
    assert len(session_id_44(sid)) == 44


def test_session_id_44_empty():
    assert session_id_44("") == ""
    assert session_id_44("   ") == ""
