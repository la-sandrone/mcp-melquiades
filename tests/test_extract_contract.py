"""
文件名: tests/test_extract_contract.py
摘要: compaction 事件 → 压缩记录的抽取契约（纯逻辑，跑合成黄金样本）。
      **这是"上游形态一变，测试先响"的核心资产之一。**
"""

from __future__ import annotations

import json
import pathlib

from melquiades.harvest.extract import (
    SUMMARY_MARKER,
    event_digest,
    event_full_text,
    extract_compactions,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "dsh_session_golden.jsonl"
CID = "11111111-2222-3333-4444-555555555555"


def _events() -> list:
    text = FIXTURE.read_text(encoding="utf-8")
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def test_extracts_exactly_one_record():
    recs = extract_compactions(_events())
    assert len(recs) == 1


def test_summary_supplied_seqs_win_over_prune():
    # prune 给 3 条、summary 给 4 条 ⇒ 采用 summary（实测 summary 才完整）。
    rec = extract_compactions(_events())[0]
    assert rec.compaction_id == CID
    assert rec.shadowed_seqs == [1, 2, 3, 4]
    assert rec.shadowed_tokens == 1234
    assert rec.shadowed_range_start == 1
    assert rec.shadowed_range_end == 12


def test_record_metadata_and_usage():
    rec = extract_compactions(_events())[0]
    assert rec.turn_no == 5
    assert rec.provider == "deepseek-official"
    assert rec.model == "deepseek-flash"
    assert rec.input_tokens == 500
    assert rec.output_tokens == 120
    assert rec.cache_read_tokens == 0


def test_summary_body_and_marker():
    rec = extract_compactions(_events())[0]
    assert SUMMARY_MARKER in rec.summary
    assert "seq→guid" in rec.summary
    # summary 事件在 fixture 的第 7 行（0-based index 6）⇒ seq == 7。
    assert rec.summary_seq == 7


def test_summary_without_start_still_recorded():
    # 跨批次场景：start 在上一批、summary 在这一批 —— 仍须产出记录，不能丢。
    evs = [e for e in _events() if e.get("type") != "compaction/start"]
    recs = extract_compactions(evs)
    assert len(recs) == 1
    assert recs[0].compaction_id == CID


def test_no_start_and_no_summary_yields_nothing():
    evs = [e for e in _events()
           if e.get("type") not in ("compaction/start", "compaction/summary")]
    assert extract_compactions(evs) == []


def test_event_digest_picks_message_body():
    ev = _events()[0]  # user/message
    assert "草稿纸" in event_digest(ev)


def test_event_full_text_collects_text_blocks():
    ev = _events()[1]  # assistant/message: reasoning + text 两个块
    full = event_full_text(ev)
    # `event_full_text` 收集的是 **`text` 字段**（ContentBlock 的正文键）；
    # reasoning 块用的是 `reasoning` 键，不在其列 —— 这是当前实现的真实行为。
    assert "已记录" in full
    # 与 event_digest 的分工：digest 只取首段，full 取全部 text 字段。
    assert event_digest(ev) == "已记录，并检索到 2 条相关旧记录。"


def test_l3_excerpts_built_for_visible_shadowed_seqs():
    rec = extract_compactions(_events(), include_excerpts=True)[0]
    # 被遮蔽的 seq 1..4 中有若干在当前事件流里可见 ⇒ 防漏网层应非空。
    assert rec.summary_excerpts
    assert rec.keywords
