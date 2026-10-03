"""
文件名: tests/test_harvest_pipeline.py
摘要: 收割全链路的黄金样本测试 —— 发现日志 → 多帧 zstd 增量读 → 事件解析 → 抽压缩记录。
      **无需数据库、无需安装 DSH**，在合成日志上跑通。
      这是"DSH 一升级，测试先报警（而不是等线上静默归零）"的核心资产。
"""

from __future__ import annotations

from melquiades.config import Config
from melquiades.harvest.drift import unknown_event_types
from melquiades.harvest.extract import extract_compactions
from melquiades.harvest.reader import (
    read_new_lines,
    session_id_from_path,
    split_lines_to_events,
)
from melquiades.harvest.runner import HarvestRunner, _gens_of_filename
from melquiades.harvest.store import HarvestStore


def _runner(cfg: Config) -> HarvestRunner:
    # 空壳 store：discover_logs 只用 cfg，不碰数据库。
    return HarvestRunner(cfg, HarvestStore(None))


def _cfg(harness) -> Config:
    cfg = Config()
    cfg.dsh.harness_dir = str(harness)
    return cfg


def test_gens_of_filename():
    assert _gens_of_filename("session.v4.jsonl.zstd") == 4
    assert _gens_of_filename("session.v10.jsonl.zstd") == 10
    assert _gens_of_filename("session.jsonl.zstd") == 0
    assert _gens_of_filename("session.lock") is None


def test_discover_picks_highest_generation(harness, session_dir, fixture_lines, write_session):
    # 同一目录内 v3（冻结）与 v4（在用）并存 ⇒ 必须取 v4。
    write_session(session_dir / "session.v3.jsonl.zstd", ['{"seq":1,"type":"session"}'])
    write_session(session_dir / "session.v4.jsonl.zstd", fixture_lines)

    logs = _runner(_cfg(harness)).discover_logs()
    assert len(logs) == 1
    assert logs[0].endswith("session.v4.jsonl.zstd")


def test_end_to_end_read_extract(harness, session_dir, fixture_lines, write_session, ids):
    write_session(session_dir / "session.v4.jsonl.zstd", fixture_lines)
    path = _runner(_cfg(harness)).discover_logs()[0]

    assert session_id_from_path(path) == ids["uuid"]

    res = read_new_lines(path, 0, 0)
    assert res.error == ""
    assert res.frames_read == len(fixture_lines)   # 逐行一帧
    assert len(res.lines) == len(fixture_lines)

    events = split_lines_to_events(res.lines)
    assert len(events) == len(fixture_lines)
    assert unknown_event_types(events) == {}       # 上游形态与已知集一致

    recs = extract_compactions(events)
    assert len(recs) == 1
    assert recs[0].compaction_id == ids["compaction_id"]
    assert recs[0].shadowed_seqs == [1, 2, 3, 4]


def test_incremental_read_respects_frame_budget(
        harness, session_dir, fixture_lines, write_session):
    path = session_dir / "session.v4.jsonl.zstd"
    write_session(path, fixture_lines)
    total = len(fixture_lines)

    r1 = read_new_lines(path, 0, max_frames=4)
    assert r1.frames_read == 4
    assert r1.stopped_early

    r2 = read_new_lines(path, r1.new_offset, 0)
    assert r2.frames_read == total - 4
    assert len(r1.lines) + len(r2.lines) == total


def test_offset_lands_on_frame_boundary_and_resumes(
        harness, session_dir, fixture_lines, write_session):
    path = session_dir / "session.v4.jsonl.zstd"
    write_session(path, fixture_lines)

    r1 = read_new_lines(path, 0, max_frames=3)
    assert 0 < r1.new_offset
    r2 = read_new_lines(path, r1.new_offset, 0)
    assert not r2.truncated      # 偏移落在帧边界 ⇒ 不需从头重读
    assert r2.error == ""


def test_truncated_file_is_detected(harness, session_dir, fixture_lines, write_session):
    path = session_dir / "session.v4.jsonl.zstd"
    write_session(path, fixture_lines)
    # 用一个远超文件长度的偏移 ⇒ 触发"文件变短"路径（truncated=True，从头重读）。
    res = read_new_lines(path, 10 ** 9, 0)
    assert res.truncated
    assert len(res.lines) == len(fixture_lines)
