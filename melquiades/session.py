"""
文件名: melquiades/session.py
摘要: 会话管理 — UUID 生成、会话隔离、换号只读。
      LLM 不知道 session_id，写操作物理上没有 GUID 参数。
依赖: 无
"""

from __future__ import annotations

import uuid
import threading
from typing import Optional


class SessionManager:
    """
    会话管理器。

    设计要点：
      - 进程启动时自动生成 UUID（LLM 不知道）
      - 所有写入自动标记 session_id
      - 换号（switch_session）后旧编号立即只读
      - 线程安全（MCP server 可能多线程调用）
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current_id: str = self._generate_uuid()
        self._previous_ids: set[str] = set()

    # ── 公开 API ──

    def get_current(self) -> str:
        """返回当前会话 ID。"""
        with self._lock:
            return self._current_id

    def switch(self) -> str:
        """
        切换到新会话。

        旧 ID 被标记为只读（加入 _previous_ids）。
        返回新 ID。
        """
        with self._lock:
            old_id = self._current_id
            self._previous_ids.add(old_id)
            self._current_id = self._generate_uuid()
            return self._current_id

    def is_current(self, session_id: str) -> bool:
        """检查 session_id 是否为当前会话。"""
        with self._lock:
            return session_id == self._current_id

    def is_readonly(self, session_id: str) -> bool:
        """
        检查 session_id 是否只读。

        只读条件：
          - 不是当前会话
          - 但曾经是（在 _previous_ids 中）
        完全未知的 session_id 也返回 True（防止写不存在的会话）。
        """
        with self._lock:
            return session_id != self._current_id

    def list_all(self) -> dict[str, str]:
        """
        返回所有会话的状态。

        返回: {"session_id": "active" | "readonly"}
        """
        with self._lock:
            result: dict[str, str] = {}
            result[self._current_id] = "active"
            for sid in self._previous_ids:
                result[sid] = "readonly"
            return result

    # ── 内部 ──

    @staticmethod
    def _generate_uuid() -> str:
        """生成 UUID v4 字符串。"""
        return str(uuid.uuid4())


# 全局单例
_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """
    获取全局会话管理器实例（懒初始化）。
    """
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager
