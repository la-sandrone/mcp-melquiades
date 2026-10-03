"""
文件名: melquiades/model.py
摘要: 数据模型 — draftpad 笔记 + draftpad_vectors 嵌入向量。
      所有字段宽度精确，类型明确，符合 stdint.h 铁律。
依赖: 无（纯 dataclasses）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


# ──────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────

MAX_CONTENT_LENGTH = 65535       # TEXT 列上限
MAX_TAGS_LENGTH = 1024           # tags JSON 序列化后上限
MAX_TAG_COUNT = 32               # 单条笔记最多标签数
MAX_TAG_LENGTH = 64              # 单个标签最长
MODEL_NAME_LENGTH = 100          # 模型名称字段长度
SESSION_ID_LENGTH = 36           # UUID v4 字符串长度
NOTE_KEY_LENGTH = 128            # LLM 自管理标识符长度
DEFAULT_TOP_K = 5                # 默认搜索返回条数


# ──────────────────────────────────────────────
# 数据模型
# ──────────────────────────────────────────────

@dataclass
class DraftpadNote:
    """
    草稿纸笔记 — 对应 draftpad 表。

    字段说明：
      id:         自增主键。0 表示"尚未持久化"（INSERT 前）。
      session_id: UUID v4，进程启动时自动生成。LLM 不知道此值。
      note_key:   LLM 自管理的业务标识符（用于 melquiades_set UPSERT）。
                 空字符串 = 未设置（melquiades_write 追加模式）。
      content:    笔记正文。TEXT 列。
      tags:       标签列表。存为 JSON 数组。
      embedding:  可选的向量嵌入。
      model_name: 生成 embedding 的模型名。
      chunk_index: 长文本切分后的块索引。
      created_at: 创建时间。
      updated_at: 最后修改时间。
      deleted_at: 软删时间戳。NULL = 未删除。
    """
    id: int = 0
    session_id: str = ""
    note_key: str = ""
    content: str = ""
    tags: list[str] = field(default_factory=list)
    embedding: Optional[list[float]] = None
    model_name: str = ""
    chunk_index: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None

    def is_deleted(self) -> bool:
        """是否已被软删除"""
        return self.deleted_at is not None

    def tag_string(self) -> str:
        """tags 序列化为 JSON 字符串（写库用）"""
        return json.dumps(self.tags[:MAX_TAG_COUNT], ensure_ascii=False)

    @classmethod
    def from_tag_string(cls, tag_json: str) -> list[str]:
        """从 JSON 字符串反序列化 tags"""
        if not tag_json:
            return []
        try:
            return json.loads(tag_json)
        except (json.JSONDecodeError, TypeError):
            return []


@dataclass
class SearchResult:
    """
    搜索结果 — 全文搜索 + 向量搜索的合并结果。

    note:        匹配的笔记。
    score:       相关性分数（FTS 或向量距离转换）。
    match_type:  'fts' | 'vector' | 'hybrid'。
    """
    note: DraftpadNote
    score: float = 0.0
    match_type: str = "fts"


@dataclass
class AuditEntry:
    """
    审计日志条目 — 记录 DELETE/UPDATE 操作。

    id:           自增主键。
    session_id:   操作发生的会话。
    record_id:    被操作的笔记 ID。
    operation:    'DELETE' | 'UPDATE'。
    old_content:  操作前的内容。
    new_content:  操作后的内容（UPDATE 时）。
    created_at:   操作时间。
    """
    id: int = 0
    session_id: str = ""
    record_id: int = 0
    operation: str = ""
    old_content: str = ""
    new_content: str = ""
    created_at: Optional[datetime] = None


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def validate_tags(tags: list[str]) -> list[str]:
    """
    验证并清理标签列表。

    规则：
      - 最多 MAX_TAG_COUNT 个
      - 每个标签最长 MAX_TAG_LENGTH
      - 去除空白
      - 去重保留顺序
    """
    seen: set[str] = set()
    cleaned: list[str] = []
    for tag in tags:
        t = tag.strip()
        if not t:
            continue
        if len(t) > MAX_TAG_LENGTH:
            t = t[:MAX_TAG_LENGTH]
        if t not in seen:
            seen.add(t)
            cleaned.append(t)
        if len(cleaned) >= MAX_TAG_COUNT:
            break
    return cleaned


def truncate_content(content: str, max_len: int = MAX_CONTENT_LENGTH) -> str:
    """截断内容到最大长度，保留完整 UTF-8 字符边界"""
    if len(content) <= max_len:
        return content
    return content[:max_len]
