"""
文件名: melquiades/harvest/__init__.py
摘要: 压缩档案收割器 —— 离线收割 DSH 会话日志的 compaction 摘要。
      子模块: reader(多帧 zstd 增量读) / extract(事件→三层内容) / store(入库) / runner(编排)
依赖: 无（各子模块自带）
"""

from melquiades.harvest.reader import (
    TailReadResult,
    read_new_lines,
    split_lines_to_events,
    session_id_from_path,
)
from melquiades.harvest.extract import (
    CompactionRecord,
    extract_compactions,
)

__all__ = [
    "TailReadResult",
    "read_new_lines",
    "split_lines_to_events",
    "session_id_from_path",
    "CompactionRecord",
    "extract_compactions",
]
