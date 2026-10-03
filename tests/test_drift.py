"""
文件名: tests/test_drift.py
摘要: DSH 事件类型漂移探测器的单元测试。
      探测器报"没见过的类型"与"少掉的正文类型"——把静默少收变成显式信号。
"""

from __future__ import annotations

from melquiades.harvest.drift import (
    CONTENT_EVENT_TYPES,
    KNOWN_EVENT_TYPES,
    MEASURED_EVENT_TYPES,
    SKIPPED_EVENT_TYPES,
    missing_content_types,
    unknown_event_types,
)


def test_measured_baseline_is_38():
    # 依据：40 个真实会话日志的事件类型直方图（2026-10），共 38 种。
    # 这条断言是"人为改动已知集"的绊索 —— 改它必须同时更新模块里的实测出处。
    assert len(MEASURED_EVENT_TYPES) == 38


def test_known_is_measured_union_skipped():
    assert KNOWN_EVENT_TYPES == MEASURED_EVENT_TYPES | SKIPPED_EVENT_TYPES
    assert SKIPPED_EVENT_TYPES <= KNOWN_EVENT_TYPES


def test_content_types_are_subset_of_known():
    assert CONTENT_EVENT_TYPES <= KNOWN_EVENT_TYPES


def test_golden_fixture_has_no_unknown_types(fixture_events):
    assert unknown_event_types(fixture_events) == {}


def test_detects_novel_event_type(fixture_events):
    evs = list(fixture_events) + [{"type": "assistant/output", "data": {}}] * 2
    hit = unknown_event_types(evs)
    assert hit == {"assistant/output": 2}


def test_missing_content_types_reports_absent():
    evs = [{"type": "user/message", "data": {}}]
    missing = missing_content_types(evs)
    assert "assistant/message" in missing
    assert "user/message" not in missing


def test_degenerate_inputs_do_not_crash():
    hit = unknown_event_types([None, {"no": "type"}, 42])
    assert hit.get("<bad-event>") == 2
    assert hit.get("<no-type>") == 1


def test_drift_scan_clean_sample(harness, session_dir, fixture_lines, write_session):
    from melquiades.cli import _drift_scan

    write_session(session_dir / "session.v4.jsonl.zstd", fixture_lines)
    agg, scanned = _drift_scan([str(session_dir / "session.v4.jsonl.zstd")], 1)
    assert scanned == 1
    assert agg == {}


def test_drift_scan_reports_novel_type(session_dir, fixture_lines, write_session):
    from melquiades.cli import _drift_scan

    novel = list(fixture_lines) + ['{"seq": 99, "type": "assistant/output", "data": {}}']
    write_session(session_dir / "session.v5.jsonl.zstd", novel)
    agg, scanned = _drift_scan([str(session_dir / "session.v5.jsonl.zstd")], 1)
    assert scanned == 1
    assert agg == {"assistant/output": 1}


def test_drift_scan_skips_unreadable_path():
    from melquiades.cli import _drift_scan

    agg, scanned = _drift_scan(["/nonexistent/session.v4.jsonl.zstd"], 1)
    assert scanned == 0
    assert agg == {}
