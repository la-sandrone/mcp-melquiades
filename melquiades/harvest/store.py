"""
文件名: melquiades/harvest/store.py
摘要: 压缩档案入库 —— 三张新表（cursor / compaction / log）的 DDL 与读写。
      · 游标推进用 **纯条件 UPDATE（CAS）**：无锁升级路径，多实例安全（实测 800 次并发无死锁）
      · 记录插入用 **INSERT IGNORE**：幂等（唯一键 compaction_id），不用 ON DUPLICATE KEY UPDATE
        （后者有已知死锁倾向：MDEV-17073 / MDEV-17603）
      · 运行日志落 dsh_harvest_log：不静默降级（铁律四）

依赖: mariadb, dbcore.py, harvest/extract.py, tokenizer.py
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from melquiades.dbcore import MariaDB
from melquiades.harvest.extract import CompactionRecord
from melquiades.tokenizer import tokenize

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# DDL（幂等）
# ──────────────────────────────────────────────

DDL_CURSOR = """
CREATE TABLE IF NOT EXISTS dsh_harvest_cursor (
    file_path    VARCHAR(512)   NOT NULL,
    byte_offset  BIGINT         NOT NULL DEFAULT 0
        COMMENT '已消耗的压缩字节偏移（多帧 zstd 帧边界）；列名不用 offset 因其为 MariaDB 保留字',
    last_seq     INT            DEFAULT NULL,
    updated_at   TIMESTAMP(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (file_path)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci
  COMMENT='收割游标：每个会话日志文件的已消耗字节偏移（多帧 zstd 帧边界）'
"""

DDL_COMPACTION = """
CREATE TABLE IF NOT EXISTS dsh_compaction (
    id                   INT            NOT NULL AUTO_INCREMENT,
    dsh_session          VARCHAR(64)    NOT NULL DEFAULT '',
    compaction_id        VARCHAR(64)    NOT NULL,
    turn_no              INT            DEFAULT NULL,
    file_path            VARCHAR(512)   NOT NULL DEFAULT '',
    occurred_at          TIMESTAMP(6)   NULL DEFAULT NULL,

    shadowed_count       INT            NOT NULL DEFAULT 0,
    shadowed_tokens      BIGINT         NOT NULL DEFAULT 0,
    shadowed_range_start INT            DEFAULT NULL,
    shadowed_range_end   INT            DEFAULT NULL,
    shadowed_seqs        MEDIUMTEXT     DEFAULT NULL
        COMMENT 'L2 回查指针：被遮蔽的事件 seq 列表（JSON 数组）',

    summary              MEDIUMTEXT     DEFAULT NULL
        COMMENT 'L1 摘要正文（**有损、可能失真**）',
    summary_chars        INT            NOT NULL DEFAULT 0,
    summary_seq          INT            DEFAULT NULL
        COMMENT 'summary 事件的 seq（回查原文用）',

    summary_excerpts     MEDIUMTEXT     DEFAULT NULL
        COMMENT 'L3 防漏网层：被遮蔽事件的首段摘要（摘要没写、原文有的词）',
    keywords             TEXT           DEFAULT NULL
        COMMENT 'L3 关键词集（jieba 切出，空格分隔）',
    content_tokenized    MEDIUMTEXT     DEFAULT NULL
        COMMENT 'jieba 分词后的检索列（摘要 + 防漏网层）',

    provider             VARCHAR(64)    NOT NULL DEFAULT '',
    model                VARCHAR(64)    NOT NULL DEFAULT '',
    input_tokens         BIGINT         NOT NULL DEFAULT 0,
    output_tokens        INT            NOT NULL DEFAULT 0,
    cache_read_tokens    BIGINT         NOT NULL DEFAULT 0,

    embedded             TINYINT(1)     NOT NULL DEFAULT 0
        COMMENT '1=已成功嵌入；0=零向量占位（VECTOR 列 =/!= 比较不可靠，一律以此列为准）',

    created_at           TIMESTAMP(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE INDEX idx_compaction_id (compaction_id),
    INDEX idx_compaction_session (dsh_session),
    INDEX idx_compaction_time (occurred_at),
    FULLTEXT INDEX ft_compaction_summary (summary),
    FULLTEXT INDEX ft_compaction_tokenized (content_tokenized)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci
  COMMENT='DSH 压缩档案：一次 compaction 一条（摘要是回查入口，不是真相）'
"""

DDL_LOG = """
CREATE TABLE IF NOT EXISTS dsh_harvest_log (
    id              INT             NOT NULL AUTO_INCREMENT,
    run_id          VARCHAR(36)     NOT NULL,
    started_at      TIMESTAMP(6)    NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    finished_at     TIMESTAMP(6)    NULL DEFAULT NULL,
    status          VARCHAR(16)     NOT NULL DEFAULT 'running'
        COMMENT 'running | ok | partial | error',
    trigger_kind    VARCHAR(16)     NOT NULL DEFAULT ''
        COMMENT 'timer | tool | startup | manual',
    files_seen      INT             NOT NULL DEFAULT 0,
    files_scanned   INT             NOT NULL DEFAULT 0,
    frames_read     INT             NOT NULL DEFAULT 0,
    events_new      INT             NOT NULL DEFAULT 0,
    compactions_new INT             NOT NULL DEFAULT 0,
    cursor_advanced INT             NOT NULL DEFAULT 0,
    error_text      TEXT            DEFAULT NULL,
    PRIMARY KEY (id),
    INDEX idx_log_started (started_at),
    INDEX idx_log_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci
  COMMENT='收割运行日志（DZ 老登方案：失败可查，绝不静默降级）'
"""

ALL_DDL = (DDL_CURSOR, DDL_COMPACTION, DDL_LOG)


# ──────────────────────────────────────────────
# 入库操作
# ──────────────────────────────────────────────

class HarvestStore:
    """压缩档案的持久化层（全部走即弃连接）。"""

    def __init__(self, db: MariaDB) -> None:
        self._db = db

    @property
    def db(self) -> MariaDB:
        """底层连接句柄（供收割后补嵌复用，避免再建一条连接）。"""
        return self._db

    # ── 建表 ──

    def ensure_tables(self) -> list[str]:
        """建三张表（幂等）。返回已确保的表名列表。"""
        created: list[str] = []
        for ddl in ALL_DDL:
            self._db.execute(ddl)
        for name in ("dsh_harvest_cursor", "dsh_compaction", "dsh_harvest_log"):
            if self._db.table_exists(name):
                created.append(name)
        return created

    # ── 游标 ──

    def get_offset(self, file_path: str) -> int:
        """读取某文件的已消耗偏移（不存在则 0）。"""
        val = self._db.scalar(
            "SELECT byte_offset FROM dsh_harvest_cursor WHERE file_path=?", (file_path,)
        )
        return int(val) if val is not None else 0

    def all_offsets(self) -> dict[str, int]:
        """全部游标（file_path → byte_offset）。"""
        rows = self._db.query("SELECT file_path, byte_offset FROM dsh_harvest_cursor")
        return {r["file_path"]: int(r["byte_offset"]) for r in rows}

    def advance_cursor(
        self, file_path: str, new_offset: int, last_seq: Optional[int] = None
    ) -> bool:
        """
        原子推进游标（CAS）。

        步骤:
          ① seed（幂等，INSERT IGNORE —— 无锁升级路径）
          ② `UPDATE ... WHERE file_path=? AND offset < ?`
             affected=1 → 本次推进成功（我赢了）
             affected=0 → 别人已推进到更远 / offset 未增长（幂等跳过）

        返回: 是否真的推进了。
        """
        self._db.execute(
            "INSERT IGNORE INTO dsh_harvest_cursor (file_path, byte_offset) VALUES (?, 0)",
            (file_path,),
        )
        n, _ = self._db.execute(
            "UPDATE dsh_harvest_cursor SET byte_offset=?, last_seq=? "
            "WHERE file_path=? AND byte_offset < ?",
            (int(new_offset), last_seq, file_path, int(new_offset)),
        )
        return n == 1

    # ── 压缩记录 ──

    def insert_compaction(
        self, rec: CompactionRecord, dsh_session: str, file_path: str
    ) -> bool:
        """
        插入一条压缩记录（幂等：唯一键 compaction_id）。

        返回: True = 新增；False = 已存在（重复，跳过）。
        """
        import json

        occurred = self._ms_to_dt(rec.occurred_at_ms)
        tokenized_src = (rec.summary or "") + "\n" + (rec.summary_excerpts or "")
        tokenized = tokenize(tokenized_src)

        sql = (
            "INSERT IGNORE INTO dsh_compaction ("
            "dsh_session, compaction_id, turn_no, file_path, occurred_at,"
            "shadowed_count, shadowed_tokens, shadowed_range_start, shadowed_range_end,"
            "shadowed_seqs, summary, summary_chars, summary_seq,"
            "summary_excerpts, keywords, content_tokenized,"
            "provider, model, input_tokens, output_tokens, cache_read_tokens,"
            "embedded"
            ") VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?,?,?, ?)"
        )
        params = (
            dsh_session,
            rec.compaction_id,
            rec.turn_no,
            file_path,
            occurred,
            rec.shadowed_count,
            rec.shadowed_tokens,
            rec.shadowed_range_start,
            rec.shadowed_range_end,
            json.dumps(rec.shadowed_seqs, ensure_ascii=False),
            rec.summary,
            len(rec.summary or ""),
            rec.summary_seq,
            rec.summary_excerpts,
            rec.keywords,
            tokenized,
            rec.provider,
            rec.model,
            rec.input_tokens,
            rec.output_tokens,
            rec.cache_read_tokens,
            0,   # embedded=0：新记录尚未嵌入；由 backfill / 定时任务补嵌后置 1
        )
        n, _ = self._db.execute(sql, params)
        return n == 1

    # ── 运行日志 ──

    def log_start(self, run_id: str, trigger_kind: str) -> None:
        """记录一次收割开始。"""
        self._db.execute(
            "INSERT INTO dsh_harvest_log (run_id, status, trigger_kind) "
            "VALUES (?, 'running', ?)",
            (run_id, trigger_kind),
        )

    def log_finish(
        self,
        run_id: str,
        status: str,
        *,
        files_seen: int = 0,
        files_scanned: int = 0,
        frames_read: int = 0,
        events_new: int = 0,
        compactions_new: int = 0,
        cursor_advanced: int = 0,
        error_text: str = "",
    ) -> None:
        """
        记录一次收割结束。

        status: ok | partial | error
        """
        self._db.execute(
            "UPDATE dsh_harvest_log SET finished_at=CURRENT_TIMESTAMP(6), "
            "status=?, files_seen=?, files_scanned=?, frames_read=?, events_new=?, "
            "compactions_new=?, cursor_advanced=?, error_text=? "
            "WHERE run_id=? AND status='running'",
            (
                status, files_seen, files_scanned, frames_read, events_new,
                compactions_new, cursor_advanced, error_text or None, run_id,
            ),
        )

    def recent_logs(self, limit: int = 20) -> list[dict]:
        """最近若干次收割记录（排查用）。"""
        return self._db.query(
            "SELECT id, run_id, started_at, finished_at, status, trigger_kind, "
            "files_seen, files_scanned, frames_read, events_new, compactions_new, "
            "cursor_advanced, error_text "
            "FROM dsh_harvest_log ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )

    def cleanup_stale_runs(self, minutes: int = 10) -> int:
        """
        把超过 `minutes` 仍为 `running` 的记录标记为 `interrupted`。

        必要性：搭车扫/定时扫跑在后台线程里，进程被 kill 时 log_finish 不会执行，
        会留下永久 running 的孤儿记录（否则日志表越来越脏且不可信）。
        返回: 修正的记录数。
        """
        n, _ = self._db.execute(
            "UPDATE dsh_harvest_log SET status='interrupted', "
            "finished_at=CURRENT_TIMESTAMP(6), "
            "error_text=COALESCE(CONCAT(error_text, ' | '), '') "
            "WHERE status='running' AND started_at < "
            "DATE_SUB(CURRENT_TIMESTAMP(6), INTERVAL ? MINUTE)",
            (int(minutes),),
        )
        if n:
            logger.info("cleanup_stale_runs: marked %d stale run(s) as interrupted", n)
        return n

    # ── 辅助 ──

    @staticmethod
    def _ms_to_dt(ms: Optional[int]) -> Optional[str]:
        """毫秒时间戳 → 'YYYY-MM-DD HH:MM:SS.ffffff'（本地时区）。"""
        if not isinstance(ms, int) or ms <= 0:
            return None
        try:
            return datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S.%f")
        except (OSError, OverflowError, ValueError):
            return None
