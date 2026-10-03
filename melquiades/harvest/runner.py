"""
文件名: melquiades/harvest/runner.py
摘要: 收割编排 —— 发现日志 → 增量读 → 提取 → 入库 → 推进游标 → 记日志。
      被三种触发复用：定时器线程 / 工具搭车 / MCP 启动扫 / CLI 手动。

纪律:
  · 单文件失败**只记录不中断**（一个坏文件不能毁掉整轮）
  · 全程遵守铁律四：任何异常都落 dsh_harvest_log，绝不静默
  · 支持 stop_event 急停：每处理一个文件前检查一次

依赖: harvest/{reader,extract,store}.py, config.py
"""

from __future__ import annotations

import logging
import os
import re as _re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from melquiades.config import Config
from melquiades.harvest.reader import (
    read_new_lines,
    session_id_from_path,
    split_lines_to_events,
)
from melquiades.harvest.extract import extract_compactions
from melquiades.harvest.message_runner import MessageStore, transcribe_one
from melquiades.harvest.store import HarvestStore

logger = logging.getLogger(__name__)


# 规范会话日志名：session.jsonl.zstd（代 0）/ session.vN.jsonl.zstd（代 N）
_SESSION_LOG_RE = _re.compile(r"^session(?:\.v([1-9][0-9]*))?\.jsonl\.zstd$")


def _gens_of_filename(name: str) -> Optional[int]:
    """规范文件名 → 日志代数；**不是**会话日志名则返回 None。

    代数进 guid ⇒ 这份文件属于哪一代，必须与全量抄录时的算法完全一致。
    """
    m = _SESSION_LOG_RE.match(name)
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else 0


def _gens_of(filename: str) -> int:
    """同上，但非会话日志名退化为 0（供 guid 计算路径使用）。"""
    return _gens_of_filename(filename) or 0


@dataclass
class HarvestStats:
    """一轮收割的统计（对应 dsh_harvest_log 的计数列）。"""

    run_id: str = ""
    trigger: str = "manual"
    files_seen: int = 0
    files_scanned: int = 0
    frames_read: int = 0
    events_new: int = 0
    compactions_new: int = 0
    # ★ 正文抄录（dsh_message / dsh_attachment）
    messages_new: int = 0
    attachments_new: int = 0
    msg_cursor_advanced: int = 0
    msg_files_scanned: int = 0
    cursor_advanced: int = 0
    skipped_no_change: int = 0
    errors: list[str] = field(default_factory=list)
    stopped: bool = False

    @property
    def status(self) -> str:
        """ok | partial | error"""
        if self.errors and self.files_scanned == 0:
            return "error"
        if self.errors:
            return "partial"
        return "ok"

    def error_text(self) -> str:
        return " | ".join(self.errors[:20])


class HarvestRunner:
    """收割编排器。**无状态**（游标在数据库里），可被多触发并发调用。"""

    def __init__(self, cfg: Config, store: HarvestStore, retriever=None) -> None:
        self._cfg = cfg
        self._store = store
        # 用于「收割后补嵌」（可选）。为 None 时会在 stats.errors 里显式记一笔，
        # 不会静默变成「看起来收割成功、其实新档案永远搜不到」。
        self._retriever = retriever
        self._last_piggyback_ts: float = 0.0
        # ★ 正文抄录用独立游标（dsh_message_cursor），与压缩收割互不干扰
        self._msg_store = MessageStore(store.db)

    # ── 文件发现 ──

    def discover_logs(self) -> list[str]:
        """
        扫描 DSH 会话目录，返回所有会话日志文件绝对路径（按 mtime 降序）。

        ★ 同一会话目录内取【代数最大】的那一份 —— 即"在用者"。
        依据（2026-09-26 修）：
          · 规范名：session.jsonl.zstd = 代 0，session.vN.jsonl.zstd = 代 N
          · 官方迁移把新代际【发布在旧文件旁】，旧文件保持字节与 inode 不变
            （本机实测 9 个目录 gen-0 与 v3 并存）⇒ 一个目录里可能有多代
          · 在用的永远是最高代际（v3→v4 后 v3 冻结、v4 才在写）。
            旧写法"按固定优先级取先命中的"会静默盯着冻结文件 ——
            看起来收割正常，实际永远读不到新数据。
          · 旧写法还把名字写死为 ("session.v3.jsonl.zstd", "session.jsonl.zstd")，
            故 v4 及以后【根本发现不了】。改为按名解析代数后，v5/v6… 无需改码。
        """
        root = self._cfg.sessions_path
        if not root.is_dir():
            logger.warning("sessions 目录不存在: %s", root)
            return []

        by_dir: dict[str, str] = {}
        for dirpath, _dirs, files in os.walk(root):
            # ★ 同一目录取【代数最大】的那一份 —— 即在用者。
            best: Optional[tuple[int, str]] = None
            for name in files:
                gens = _gens_of_filename(name)
                if gens is None:
                    continue          # 非会话日志名（lock / tmp / 其它）一律不看
                if best is None or gens > best[0]:
                    best = (gens, name)
            if best is not None:
                by_dir.setdefault(dirpath, os.path.join(dirpath, best[1]))

        found = list(by_dir.values())
        try:
            found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        except OSError:
            pass
        return found

    # ── 收割后补嵌 ──

    def embed_pending(self, table: str = "dsh_compaction", limit: int = 0) -> dict:
        """
        为 embedded=0 的行补向量（dsh_compaction 与 draftpad 通用）。

        为什么必须在这里做：insert_compaction **不写 embedding**（新行落
        DEFAULT 零向量占位），若只靠人工 backfill，新收割的档案会长期
        检索不到 —— 2026-09-22 实测的 29/103 行零向量正是这么积下来的。
        """
        from melquiades.backfill import backfill_embeddings

        if self._retriever is None:
            return {"skipped_no_retriever": 1}
        text_col = "content" if table == "draftpad" else "summary"
        return backfill_embeddings(
            self._store.db, self._retriever.embed, table=table,
            text_col=text_col, limit=limit,
        )

    # ── 主流程 ──

    def run_once(
        self,
        trigger: str = "manual",
        *,
        max_files: int = 0,
        max_frames_per_file: int = 0,
        stop_event: Optional[threading.Event] = None,
        write_log: bool = True,
    ) -> HarvestStats:
        """
        跑一轮收割。

        参数:
          trigger              — timer | tool | startup | manual（记入日志表）
          max_files            — 本轮最多处理多少文件（0=不限）；启动扫用它限速
          max_frames_per_file  — 单个文件本轮最多解压多少帧（0=不限）
          stop_event           — 急停信号；每处理一个文件前检查
          write_log            — 是否写 dsh_harvest_log（测试可关）

        返回: HarvestStats
        """
        stats = HarvestStats(run_id=str(uuid.uuid4()), trigger=trigger)

        if not self._cfg.dsh.harvest_enabled:
            stats.errors.append("harvest disabled by config")
            return stats

        if write_log:
            try:
                self._store.log_start(stats.run_id, trigger)
            except Exception as exc:  # 日志表不可用也不能阻断收割
                logger.error("log_start failed: %s", exc)

        logs = self.discover_logs()
        stats.files_seen = len(logs)
        if max_files and len(logs) > max_files:
            logs = logs[:max_files]

        for path in logs:
            if stop_event is not None and stop_event.is_set():
                stats.stopped = True
                break
            try:
                self._harvest_one(path, stats, max_frames_per_file)
            except Exception as exc:
                # 单文件失败：记录并继续（铁律四）
                msg = f"{os.path.basename(os.path.dirname(path))}: {type(exc).__name__}: {exc}"
                stats.errors.append(msg)
                logger.error("harvest failed for %s: %s", path, exc)

        # ★ 正文抄录（第二消费方）：独立游标、独立容错。
        #   隔离成独立循环的理由：正文侧的任何失败都不许拖垮压缩侧
        #   （反之亦然）—— 两个消费方读同一批字节，但各有各的账本。
        for path in logs:
            if stop_event is not None and stop_event.is_set():
                stats.stopped = True
                break
            try:
                self._transcribe_one(path, stats, max_frames_per_file)
            except Exception as exc:  # noqa: BLE001
                msg = f"msg:{os.path.basename(os.path.dirname(path))}: {type(exc).__name__}: {exc}"
                stats.errors.append(msg)
                logger.error("transcribe failed for %s: %s", path, exc)

        if write_log:
            try:
                self._store.log_finish(
                    stats.run_id,
                    stats.status,
                    files_seen=stats.files_seen,
                    files_scanned=stats.files_scanned,
                    frames_read=stats.frames_read,
                    events_new=stats.events_new,
                    compactions_new=stats.compactions_new,
                    cursor_advanced=stats.cursor_advanced,
                    error_text=stats.error_text(),
                )
            except Exception as exc:
                logger.error("log_finish failed: %s", exc)

        logger.info(
            "harvest[%s] files=%d/%d frames=%d events=%d compactions=%d advanced=%d errors=%d",
            trigger, stats.files_scanned, stats.files_seen, stats.frames_read,
            stats.events_new, stats.compactions_new, stats.cursor_advanced,
            len(stats.errors),
        )

        # ★ v2 指针同步（seq 指针 → guid 指针）。**必须挂在这里**：
        #   v2 的 shadowed_guids 要查 dsh_message 才能译出，而正文侧
        #   （_transcribe_one）刚刚才跑完。若挂在压缩侧（insert_compaction），
        #   会因两个消费方的执行顺序不定而查不到 guid —— 顺序恰好颠倒时就永久留下
        #   空指针（静默降级，正是本项目最忌讳的那种失败）。
        #   放在收尾 + not stopped，与补嵌同一纪律：急停时不做事。
        if not stats.stopped:
            try:
                synced = self.sync_compaction_v2()
                if synced.get("written"):
                    logger.info("compaction_v2 sync: %s", synced)
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(
                    f"compaction_v2 sync: {type(exc).__name__}: {exc}")

        # 收割后补嵌（急停时不补，避免停机时还在发 HTTP）：
        #   ① dsh_compaction —— 新收割的压缩档案（insert 不写向量）
        #   ② draftpad      —— 写入/覆盖/追加路径同样不算向量，每次顺带清一遍
        #      （`WHERE embedded=0` 只扫未嵌行，代价可忽略）
        if not stats.stopped:
            if self._retriever is None:
                if stats.compactions_new > 0:
                    stats.errors.append("post-harvest embed skipped: no retriever")
            else:
                for t in ("dsh_compaction", "draftpad"):
                    if t == "dsh_compaction" and stats.compactions_new <= 0:
                        continue
                    try:
                        res = self.embed_pending(table=t)
                        if res.get("total"):
                            logger.info("post-harvest embed[%s]: %s", t, res)
                    except Exception as exc:
                        stats.errors.append(
                            f"post-harvest embed[{t}] failed: {type(exc).__name__}: {exc}")
                        logger.error("post-harvest embed[%s] failed: %s", t, exc)
        return stats

    # ── 单文件 ──

    def _harvest_one(
        self, path: str, stats: HarvestStats, max_frames_per_file: int
    ) -> None:
        """处理单个日志文件（读→提取→入库→推进游标）。"""
        offset = self._store.get_offset(path)
        budget = max_frames_per_file or self._cfg.dsh.max_frames_per_cycle

        result = read_new_lines(path, offset, max_frames=budget)
        if result.error:
            raise RuntimeError(result.error)

        if not result.lines and result.new_offset == offset:
            stats.skipped_no_change += 1
            return

        stats.files_scanned += 1
        stats.frames_read += result.frames_read
        stats.events_new += len(result.lines)

        if result.lines:
            events = split_lines_to_events(result.lines)
            recs = extract_compactions(
                events,
                excerpt_chars=self._cfg.harvest.excerpt_chars,
                keywords_max=self._cfg.harvest.keywords_max,
                shadowed_seqs_max=self._cfg.harvest.shadowed_seqs_max,
                summary_max_chars=self._cfg.harvest.summary_max_chars,
            )
            sid = session_id_from_path(path)
            for rec in recs:
                if self._store.insert_compaction(rec, sid, path):
                    stats.compactions_new += 1

        if result.new_offset > offset:
            if self._store.advance_cursor(path, result.new_offset):
                stats.cursor_advanced += 1

    # ── 正文抄录（独立消费方） ──

    def _transcribe_one(
        self, path: str, stats: HarvestStats, max_frames_per_file: int
    ) -> None:
        """
        增量抄录单个日志的正文 → dsh_message / dsh_attachment。

        与 _harvest_one 并行的第二消费方：
          · 读的是同一批字节，但游标是 dsh_message_cursor（各自记账）
          · 失败只记 stats.errors，不影响压缩侧（调用方已隔离）
        """
        import os as _os

        budget = max_frames_per_file or self._cfg.dsh.max_frames_per_cycle
        gens = _gens_of(_os.path.basename(path))
        res = transcribe_one(path, gens, self._msg_store, max_frames=budget)
        if res.get("skipped_no_change"):
            return
        stats.msg_files_scanned += 1
        stats.messages_new += int(res.get("rows", 0))
        stats.attachments_new += int(res.get("attachments", 0))
        if res.get("rows") or res.get("attachments"):
            stats.msg_cursor_advanced += 1
        for e in res.get("errors", [])[:5]:
            stats.errors.append(f"msg:{e}")

    # ── v2 指针同步（seq → guid） ──

    def sync_compaction_v2(self, limit: int = 200) -> dict:
        """把缺 v2 记录（或 guid 指针为空）的压缩档案补齐。幂等，可重复跑。

        只挑同时满足两条的档案：
          ① v2 里没有它，或它的 shadowed_guids 为空 —— 即尚未拿到 guid 指针
          ② 该会话在 dsh_message 里**确有正文** —— 否则译不出 guid

        条件②不是可有可无的优化：它让那批「源日志与正文都已不在」的历史档案
        （实测 18 条）不再每轮重试 —— 它们永远译不出 guid，重试只是白烧 DB 往返。

        返回 {"examined": n, "written": n, "empty": n, "skipped": n}
        """
        from melquiades.harvest.compaction_v2 import insert_v2_row, translate

        db = self._store._db            # noqa: SLF001 —— 同包内受控访问（同 message_runner）
        # dsh_session 可能是裸 uuid，也可能是 session-<uuid>，故条件用 IF 归一
        sql = (
            "SELECT v1.id, v1.dsh_session, v1.compaction_id, v1.turn_no, v1.file_path,"
            "       v1.shadowed_count, v1.shadowed_tokens, v1.shadowed_seqs, v1.summary"
            " FROM dsh_compaction v1"
            " WHERE EXISTS ("
            "         SELECT 1 FROM dsh_message m"
            "         WHERE m.session_id = CONCAT("
            "                 IF(v1.dsh_session LIKE 'session-%', '', 'session-'),"
            "                 v1.dsh_session))"
            "   AND NOT EXISTS ("
            "         SELECT 1 FROM dsh_compaction_v2 v2"
            "         WHERE v2.compaction_id = v1.compaction_id"
            "           AND v2.shadowed_guids IS NOT NULL"
            "           AND v2.shadowed_guids <> '[]')"
            " ORDER BY v1.id LIMIT %s"
        )
        with db.cursor(dictionary=True) as cur:
            # 参数绑定而非 % 拼接 —— SQL 里含字面量 LIKE 'session-%'，
            # 用 "..." % limit 会把 %' 当格式符炸掉。
            cur.execute(sql, (int(limit),))
            candidates = cur.fetchall()

        res = {"examined": len(candidates), "written": 0, "empty": 0, "skipped": 0}
        for row in candidates:
            compiled = translate(db, row)
            if "skip_reason" in compiled["diag"]:
                res["skipped"] += 1
                continue
            if not compiled["shadowed_guids"]:
                res["empty"] += 1
                continue
            insert_v2_row(db, compiled, replace=True)
            res["written"] += 1
        return res

    # ── 便捷：启动增量扫 / 搭车 ──

    def startup_scan(self, stop_event: Optional[threading.Event] = None) -> HarvestStats:
        """
        MCP 加载时扫一轮 —— **只做增量**（限文件数）。

        首次全量扫（游标表为空）不应走这里（170MB 会拖住启动），
        由 CLI `--backfill` 手动预跑。此处用 `startup_scan_dirs_limit` 限速。
        """
        return self.run_once(
            "startup",
            max_files=int(self._cfg.dsh.startup_scan_dirs_limit),
            stop_event=stop_event,
        )

    def tool_scan(self, stop_event: Optional[threading.Event] = None) -> Optional[HarvestStats]:
        """
        工具搭车扫（fire-and-forget 调用方请在后台线程里调）。

        **带节流**：距上次搭车不足 `piggyback_min_interval_sec` 秒则直接返回 None。
        理由：每次工具调用都做一轮 os.walk(61 文件) 是无谓开销，
              且会产出大量'无变化'的日志记录。
        """
        import time as _time

        now = _time.time()
        gap = now - self._last_piggyback_ts
        if gap < float(self._cfg.dsh.piggyback_min_interval_sec):
            logger.debug("piggyback throttled (%.1fs < %ds)", gap,
                         self._cfg.dsh.piggyback_min_interval_sec)
            return None
        self._last_piggyback_ts = now
        return self.run_once("tool", max_files=0, stop_event=stop_event)

    def backfill(
        self, stop_event: Optional[threading.Event] = None
    ) -> HarvestStats:
        """
        首扫：全量（受 backfill_* 配置限制）。

        **不进启动路径** —— 由 CLI 手动执行一次。
        """
        return self.run_once(
            "manual",
            max_files=int(self._cfg.dsh.backfill_max_files),
            max_frames_per_file=int(self._cfg.dsh.backfill_max_frames_per_file),
            stop_event=stop_event,
        )
