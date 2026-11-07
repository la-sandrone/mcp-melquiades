"""
文件名: melquiades/session_meta.py
摘要: 会话元信息（工作区 / 会话标题）—— 把「session-xxxx」变成人能认的东西。

为什么需要它（2026-11-07）：
  · 检索结果里只有裸 session_id，人看不懂、LLM 也判断不了「这是哪一场」；
  · 侧边栏要显示「工作区 › 会话标题」，context 抬头也要带，否则「找谁」全靠猜；
  · 工作区加权（DESIGN.md #15）需要 session → workspace 的映射。

数据源与代价（实测）：
  · `storages/workspace.json`（16 KB / 个 home）→ `{path, title, sessionIds[]}`
    ⇒ **全量加载**，覆盖 DB 会话的 86.2% 行（19 个工作区 / 170 会话）。
  · `storages/session_projcache/sessions/<sid>.json` → `record.rows.title.val`
    ⇒ **懒加载 + LRU**：单个文件最大 1.8 MB，全量 64 MB；只有「真正要展示的那几条」
      才需要标题，所以按需解析、解析结果缓存。
  · 两者都读不到时返回空 —— **绝不猜**（显示「未分类工作区」，且不参与 boost）。

★ 不写库、不收割：这是**读 DSH 的存储**，与「只读、不侵入上游」的纪律一致。
  没有把标题/工作区落成表，是因为它们是 DSH 的派生数据、会随会话改名而变；
  真要落表，得先解决「谁更新」的问题（那是另一件事，见 DESIGN.md #15 的待实施项）。

依赖: 无（仅标准库）
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 标题文件超过这个大小就不解析了（实测最大 1.8 MB；4 MB 是防呆线，不是标定值）
_MAX_TITLE_FILE_BYTES = 4 * 1024 * 1024
# 标题缓存上限（每条结果页最多几十个会话，256 足够；防止长跑进程吃内存）
_TITLE_CACHE_MAX = 256

UNKNOWN_WORKSPACE = "未分类工作区"


def normalize_session_id(sid: str) -> str:
    """统一成带 `session-` 前缀的形式。

    ★ 必须归一：`dsh_message.session_id` 有 6,859 行是**裸 uuid**，
      而 workspace.json / projcache 的文件名都带前缀。不归一会表现为
      「明明有这场会话，工作区却显示未分类」。
    """
    s = (sid or "").strip()
    if not s:
        return ""
    return s if s.startswith("session-") else "session-" + s


@dataclass
class SessionMeta:
    session_id: str
    workspace_title: str = ""
    workspace_path: str = ""
    title: str = ""

    @property
    def workspace_label(self) -> str:
        return self.workspace_title or UNKNOWN_WORKSPACE

    def describe(self) -> str:
        """人类可读的一行：`工作区 › 标题 · session-xxxx`。"""
        bits = [self.workspace_label]
        if self.title:
            bits.append(self.title)
        return " › ".join(bits) + " · " + self.session_id


class SessionMetaIndex:
    """会话元信息索引。**失败一律降级为空，绝不抛**（检索不能因为元信息挂掉）。"""

    def __init__(self, homes=None) -> None:
        self._homes: list[Path] = [Path(h) for h in (homes or [])]
        self._sid_to_ws: dict[str, tuple[str, str]] = {}   # sid -> (title, path)
        self._title_cache: dict[str, str] = {}
        self._loaded = False

    # ── 发现 ──

    @classmethod
    def from_dsh_home(cls, dsh_home=None, harness_dir=None) -> "SessionMetaIndex":
        """由 DSH home / harness_dir 发现所有带 storages 的 home。

        ★ 为什么要 harness_dir 兜底：`cfg.dsh_home` 是**由 DSH_VERSION 派生**的
          （见 config.resolve_dsh_home），在 MCP 进程外跑（工具、测试、离线脚本）
          拿不到该环境变量 ⇒ 会回落到 `~/.dsh`（不存在）⇒ 索引静默为空。
          这正是 config.yaml 里记的 2026-09-26 事故的同一类坑：
          **追规则（harness_dir/dsh-domus*），不追名单（写死某个版本目录）**。
        """
        homes: list[Path] = []
        seen: set[str] = set()

        def _add(p: Path) -> None:
            try:
                key = str(p.resolve())
            except OSError:
                return
            if key not in seen and (p / "storages").is_dir():
                seen.add(key)
                homes.append(p)

        if dsh_home:
            _add(Path(dsh_home))
        if harness_dir:
            try:
                for cand in sorted(Path(harness_dir).glob("dsh-domus*")):
                    _add(cand)
            except OSError:
                pass
        return cls(homes)
    # ── 加载 ──

    def ensure_loaded(self) -> None:
        if self._loaded:
            return
        for home in self._homes:
            self._load_workspace_json(home)
        self._loaded = True
        logger.info("session meta loaded: %d sessions mapped, homes=%d",
                    len(self._sid_to_ws), len(self._homes))

    def _load_workspace_json(self, home: Path) -> None:
        f = home / "storages" / "workspace.json"
        try:
            if not f.is_file():
                return
            with f.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            workspaces = (data.get("tables") or {}).get("workspaces") or {}
            for _wid, w in workspaces.items():
                title = str(w.get("title") or "")
                wpath = str(w.get("path") or "")
                for sid in (w.get("sessionIds") or []):
                    self._sid_to_ws[normalize_session_id(sid)] = (title, wpath)
        except Exception as exc:  # noqa: BLE001 —— 元信息读不到就降级，绝不拖垮检索
            logger.warning("workspace.json load failed (%s): %s", f, exc)

    def _load_title(self, sid: str) -> str:
        """懒加载单个会话标题（带 LRU 上限）。"""
        if sid in self._title_cache:
            return self._title_cache[sid]
        title = ""
        for home in self._homes:
            f = home / "storages" / "session_projcache" / "sessions" / (sid + ".json")
            try:
                if not f.is_file():
                    continue
                if f.stat().st_size > _MAX_TITLE_FILE_BYTES:
                    logger.debug("title file too large, skipped: %s", f)
                    continue
                with f.open("r", encoding="utf-8") as fh:
                    rec = (json.load(fh) or {}).get("record") or {}
                rows = rec.get("rows") or {}
                title = str((rows.get("title") or {}).get("val") or "")
                if not title:
                    ident = rec.get("identity") or {}
                    title = ""
                break
            except Exception as exc:  # noqa: BLE001
                logger.debug("title load failed (%s): %s", f, exc)
        if len(self._title_cache) >= _TITLE_CACHE_MAX:
            self._title_cache.clear()
        self._title_cache[sid] = title
        return title

    # ── 查询 ──

    def get(self, session_id: str, with_title: bool = True) -> SessionMeta:
        self.ensure_loaded()
        sid = normalize_session_id(session_id)
        ws_title, ws_path = self._sid_to_ws.get(sid, ("", ""))
        title = self._load_title(sid) if with_title else ""
        return SessionMeta(session_id=sid, workspace_title=ws_title,
                           workspace_path=ws_path, title=title)

    def workspace_of(self, session_id: str) -> str:
        """返回工作区**路径**（比较用；标题只是展示）。找不到返回空串。"""
        self.ensure_loaded()
        return self._sid_to_ws.get(normalize_session_id(session_id), ("", ""))[1]


_INDEX: Optional[SessionMetaIndex] = None


def get_index(dsh_home=None, harness_dir=None) -> SessionMetaIndex:
    """进程级单例（MCP 常驻，加载一次即可）。

    首次调用时的 (dsh_home, harness_dir) 决定扫描范围；之后复用。
    `harness_dir` 必须有 —— 见 from_dsh_home 的说明（DSH_VERSION 在进程外取不到）。
    """
    global _INDEX
    if _INDEX is None:
        _INDEX = SessionMetaIndex.from_dsh_home(dsh_home, harness_dir)
    return _INDEX
