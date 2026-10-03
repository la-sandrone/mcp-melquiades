"""
文件名: melquiades/harvest/message_runner.py
摘要: dsh_message / dsh_attachment 的【增量】抄录 —— 逻辑同扫压缩：
      逐文件字节偏移游标 + 多帧 zstd 增量读 + CAS 推进 + 分批提交。

三触发由 HarvestRunner 统一提供（timer / startup / 工具搭车），本模块只管
「单文件增量抄录」这一件事。

★ 游标与 dsh_harvest_cursor **分离**：两个消费方读同一批字节，若共用游标，
  先推进者会让另一方永久错过数据。故各自一套。

★ line_no 契约（与全量抄录同一坐标系，guid 才稳定）：
  guid 里的 line_no = 解压后整条流的行号（0-based，**空行与坏行也占号**）。
  多帧 zstd 的帧边界对齐行边界（实测），故每帧的 `data.count(b"\n")` 之和
  即为该帧贡献的行号数 ⇒ 增量读能把行号接得天衣无缝。

依赖: harvest/{reader,message_extract}.py
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import zstandard as zstd

from melquiades.harvest.message_extract import messages_from_events
from melquiades.harvest.reader import session_id_from_path

logger = logging.getLogger(__name__)

BATCH_ROWS = 500


@dataclass
class DeltaResult:
    """一次增量读取的结果。index i ↔ 解压后第 i 行（obj 或 None）。"""

    lines: list = field(default_factory=list)
    new_offset: int = 0
    line_count: int = 0          # 本批消费的行号数（含空行/坏行）
    frames_read: int = 0
    stopped_early: bool = False
    truncated: bool = False
    error: str = ""


def read_delta(path: str, offset: int = 0, max_frames: int = 0) -> DeltaResult:
    """从 offset 起增量解压多帧 zstd，保持行号契约。

    与 harvest/reader.read_new_lines 的差别：**不丢弃空行/坏行**，
    而是以 None 占位，使返回列表的下标严格等于解压后的行号。
    （read_new_lines 过滤空行后会丢失下标信息，故不能直接复用。）
    """
    import json

    result = DeltaResult(new_offset=offset)
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        result.error = f"stat failed: {exc}"
        return result

    start = offset
    if offset > size:
        start = 0
        result.truncated = True      # 文件被截断/重写 —— 从头来，且显式标注

    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            raw = fh.read()
    except OSError as exc:
        result.error = f"open/read failed: {exc}"
        return result

    pos = 0
    frames = 0
    consumed_lines = 0
    while pos < len(raw):
        if max_frames and frames >= max_frames:
            result.stopped_early = True
            break
        dobj = zstd.ZstdDecompressor().decompressobj()
        try:
            data = dobj.decompress(raw[pos:])
        except zstd.ZstdError:
            break                    # 半帧（正在写入）—— offset 停在此帧之前
        unused = dobj.unused_data or b""
        consumed = len(raw) - pos - len(unused)
        if consumed <= 0:
            break
        pos += consumed
        frames += 1

        if not data:
            continue
        if data.endswith(b"\n"):
            parts = data.split(b"\n")[:-1]
        else:
            # 帧未以换行结尾（理论上不会发生：实测帧边界对齐行边界）。
            # 不静默吞掉，记一笔待查。
            parts = data.split(b"\n")
            logger.warning("frame not newline-terminated in %s (frames=%d)", path, frames)
        for line in parts:
            try:
                obj = json.loads(line)
                result.lines.append(obj if isinstance(obj, dict) else None)
            except Exception:
                result.lines.append(None)        # 坏行占位，保住行号
            consumed_lines += 1

    result.frames_read = frames
    result.new_offset = start + pos
    result.line_count = consumed_lines
    return result


class MessageStore:
    """dsh_message / dsh_attachment / dsh_message_cursor 的读写（即弃连接）。"""

    def __init__(self, db) -> None:            # db: melquiades.dbcore.MariaDB
        self._db = db

    def get_cursor(self, log_path: str) -> tuple[int, int]:
        """返回 (byte_offset, line_base)；无记录则 (0, 0)。"""
        row = self._db.query(
            "SELECT byte_offset, line_base FROM dsh_message_cursor WHERE log_path=?",
            (log_path,),
        )
        if not row:
            return 0, 0
        return int(row[0]["byte_offset"]), int(row[0]["line_base"])

    def advance_cursor(self, log_path: str, offset: int, line_base: int,
                       file_bytes: int) -> bool:
        """CAS 推进：仅当新偏移更大时才覆盖（防并发回退）。"""
        with self._db.cursor(readonly=False) as cur:
            cur.execute(
                """INSERT INTO dsh_message_cursor (log_path, byte_offset, line_base, file_bytes)
                   VALUES (?,?,?,?)
                   ON DUPLICATE KEY UPDATE
                     byte_offset = GREATEST(byte_offset, VALUES(byte_offset)),
                     line_base   = GREATEST(line_base,   VALUES(line_base)),
                     file_bytes  = VALUES(file_bytes)""",
                (log_path, int(offset), int(line_base), int(file_bytes)),
            )
            return cur.rowcount > 0

    def insert_messages(self, rows: list[dict], session_id: str, gens: int,
                        cwd: str | None) -> int:
        if not rows:
            return 0
        n = 0
        with self._db.cursor(readonly=False) as cur:
            for i in range(0, len(rows), BATCH_ROWS):
                batch = rows[i:i + BATCH_ROWS]
                cur.executemany(
                    """INSERT INTO dsh_message
                       (guid, session_id, source_gens, line_no, block_path, seq, ev_type,
                        kind, role, turn, ev_time, cwd, ordinal, is_shadowed, is_index,
                        body, body_sha256, body_bytes)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)
                       ON DUPLICATE KEY UPDATE body=VALUES(body), ev_time=VALUES(ev_time),
                         body_sha256=VALUES(body_sha256), body_bytes=VALUES(body_bytes)""",
                    [(r["guid"], session_id, gens, r["line_no"], r["block_path"], r["seq"],
                      r["ev_type"], r["kind"], r["role"], r["turn"], r["ev_time"], cwd,
                      r["ordinal"], 1 if "\u2591" in (r["body"] or "") else 0,
                      r["body"], r["body_sha256"], r["body_bytes"]) for r in batch],
                )
                n += len(batch)
        return n

    def insert_attachments(self, atts: list[dict], session_id: str, gens: int) -> int:
        if not atts:
            return 0
        with self._db.cursor(readonly=False) as cur:
            cur.executemany(
                """INSERT INTO dsh_attachment
                   (attachment_id, sha256, session_id, source_gens, line_no, block_path,
                    media_type, name, bytes, width, height, local_copy_path, copy_state)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,'none')
                   ON DUPLICATE KEY UPDATE bytes=VALUES(bytes),
                     media_type=VALUES(media_type), name=VALUES(name)""",
                [(a["attachment_id"], a["sha256"], session_id, gens, a["line_no"],
                  a["block_path"], a["media_type"], a["name"], a["bytes"],
                  a["width"], a["height"]) for a in atts],
            )
        return len(atts)


def transcribe_one(
    path: str,
    gens: int,
    store: MessageStore,
    max_frames: int = 0,
    stop_event=None,
) -> dict:
    """
    增量抄录单个日志文件。返回统计 dict（供 HarvestStats 汇总）。

    纪律：单文件失败由调用方捕获并记录（一个坏文件不能毁掉整轮）。
    未知/异常一律**上报**，不静默。
    """
    stats = {"rows": 0, "attachments": 0, "frames": 0, "dup_skipped": 0,
             "skipped_no_change": False, "errors": []}

    offset, line_base = store.get_cursor(path)
    res = read_delta(path, offset, max_frames=max_frames)
    if res.error:
        stats["errors"].append(res.error)
        return stats
    if not res.lines and res.new_offset == offset:
        stats["skipped_no_change"] = True
        return stats
    if stop_event is not None and stop_event.is_set():
        stats["errors"].append("stopped before processing")
        return stats

    stats["frames"] = res.frames_read

    # header 只需要一次：取本批第一个可解析事件；若本批不含 header（增量批次），
    # 依次回落到【台账】与【路径】—— 三级都不猜、不编。
    header = next((o for o in res.lines if isinstance(o, dict)), None)
    session_id = header.get("id") if isinstance(header, dict) else None
    cwd = header.get("cwd") if isinstance(header, dict) else None
    if not session_id:
        row = store._db.query(          # noqa: SLF001 —— 同包内的受控访问
            "SELECT session_id FROM dsh_source_log WHERE log_path=?", (path,))
        session_id = row[0]["session_id"] if row else None
    if not session_id:
        # ③ 从【路径】取 —— 目录名 session-<uuid> 本身就是权威来源，
        #    压缩侧（runner._harvest_one）一直这么取，正文侧原先漏了这一级。
        #
        #    为什么台账不能当唯一兜底（2026-09-26 实测）：dsh_source_log 由全量
        #    抄录器维护，只覆盖"跑过全量的 root"。改名/换 root 之后新 root 的日志
        #    全部无记录 —— 实测改名后 66 个日志在新 root，台账里 0 条。此时若
        #    header 又不在增量批次里（游标已推进），就必然报"取不到 session_id"。
        derived = session_id_from_path(path)
        if derived:
            # session_id_from_path 返回的是 uuid 部分（36 字符），而本表契约是
            # CHAR(44)、且 header.id 形如 session-<uuid>（44）。必须补前缀 ——
            # 否则同一会话会在库里分裂成 36/44 两种 id（实测已有 4 会话残留）。
            session_id = derived if derived.startswith("session-") else f"session-{derived}"
    if not session_id:
        stats["errors"].append(
            f"{os.path.basename(os.path.dirname(path))}: 取不到 session_id"
            f"（header 不在本批、台账无记录、路径也解析不出）")
        return stats

    rows, atts, ex = messages_from_events(res.lines, session_id, gens, line_base)
    if ex["bad_attachment_ids"]:
        stats["errors"].extend(ex["bad_attachment_ids"][:5])
    stats["dup_skipped"] = ex["dup_skipped"]

    stats["rows"] = store.insert_messages(rows, session_id, gens, cwd)
    stats["attachments"] = store.insert_attachments(atts, session_id, gens)
    if res.new_offset > offset or res.line_count:
        store.advance_cursor(path, res.new_offset, line_base + res.line_count,
                             os.path.getsize(path))
    return stats
