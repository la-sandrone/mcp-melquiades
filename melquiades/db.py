"""
文件名: melquiades/db.py
摘要: MariaDB 连接管理 — 连接池、自动建表、CRUD、UPSERT、审计日志。
      使用 mariadb 官方连接器（C 扩展，支持 VECTOR 类型）。

      v2.0: 新增 content_tokenized 列 + jieba 分词支持，
            解决 MariaDB InnoDB FULLTEXT 对 CJK（中文/日文/韩文）
            分词支持薄弱的问题。
依赖: mariadb, jieba（可选）, model.py
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import mariadb

from melquiades.model import (
    DraftpadNote,
    AuditEntry,
    MAX_CONTENT_LENGTH,
    MAX_TAGS_LENGTH,
    MAX_TAG_COUNT,
    SESSION_ID_LENGTH,
    NOTE_KEY_LENGTH,
    MODEL_NAME_LENGTH,
)

logger = logging.getLogger(__name__)

# ── jieba 分词（可选依赖）──
try:
    import jieba
except ImportError:
    jieba = None  # type: ignore[assignment]
    logger.info("jieba not installed — CJK FULLTEXT tokenization disabled")


# ── CJK 检测（与 search.py 重复定义，保持模块独立）──
_CJK_RE = re.compile(
    r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff'
    r'\u3000-\u303f\uff00-\uffef]'
)


def _has_cjk(text: str) -> bool:
    """检测文本是否包含 CJK 字符"""
    return bool(_CJK_RE.search(text))


# ──────────────────────────────────────────────
# DDL 语句 — 建表
# ──────────────────────────────────────────────

# ⚠️ 修改本文件里任何列定义前必读：
#
#   表上带 FULLTEXT 索引时，`ALTER TABLE ... MODIFY COLUMN`（**哪怕只改 COMMENT**）
#   只能走 `ALGORITHM=COPY` —— 会重建整表 + 重建 FTS 索引。
#   `INSTANT` / `NOCOPY` 都会被拒，错误是：
#       ERROR 1846: ALGORITHM=INSTANT is not supported.
#       Reason: InnoDB presently supports one FULLTEXT index creation at a time.
#
#   实测对照：无 FULLTEXT 的表改注释 = INSTANT，**25.9 ms**；
#             有 FULLTEXT 的 17 GB 表 = 1–1.5 小时。
#
#   所以：**改注释不要单独跑 DDL**，攒到下次必须重建（改列类型/加索引）时
#   合并进同一条 ALTER。也别想"先删 FULLTEXT 再改"——删 FULLTEXT 自己也要
#   rebuild（实测 91 分钟）。
#
#   另注：`draftpad` 的 content_tokenized 注释已是新文案（这张表小，改过）；
#   但 memoria 库的 `documents_book_chunk` 同名列表**有意未改**，等下次重建。
#
DDL_DRAFTPAD = """
CREATE TABLE IF NOT EXISTS draftpad (
    id                  INT             NOT NULL AUTO_INCREMENT,
    session_id          VARCHAR(36)     NOT NULL,
    note_key            VARCHAR(128)    NOT NULL DEFAULT '',
    content             TEXT            NOT NULL,
    content_tokenized   MEDIUMTEXT      DEFAULT NULL
        COMMENT 'CJK unigram+bigram（2026-10-25 起；原 TEXT 装不下膨胀后的串）',
    tags                VARCHAR(1024)   DEFAULT '[]',
    model_name          VARCHAR(100)    DEFAULT '',
    chunk_index         INT             DEFAULT 0,
    created_at          TIMESTAMP(6)    NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at          TIMESTAMP(6)    NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    deleted_at          TIMESTAMP(6)    NULL DEFAULT NULL,
    PRIMARY KEY (id),
    INDEX idx_draftpad_session (session_id),
    INDEX idx_draftpad_deleted (deleted_at),
    UNIQUE INDEX idx_draftpad_key (session_id, note_key),
    FULLTEXT INDEX ft_draftpad_content (content),
    FULLTEXT INDEX ft_draftpad_tokenized (content_tokenized)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;
"""

# ── 已有表迁移 DDL（幂等）──

DDL_MIGRATE_TOKENIZED_COLUMN = """
ALTER TABLE draftpad
    ADD COLUMN IF NOT EXISTS content_tokenized MEDIUMTEXT DEFAULT NULL
        COMMENT 'CJK unigram+bigram（2026-10-25 起；原 TEXT 装不下膨胀后的串）'
"""

DDL_MIGRATE_TOKENIZED_INDEX = """
ALTER TABLE draftpad
    ADD FULLTEXT INDEX IF NOT EXISTS ft_draftpad_tokenized (content_tokenized)
"""


DDL_DRAFTPAD_VECTORS = """
CREATE TABLE IF NOT EXISTS draftpad_vectors (
    id          INT             NOT NULL AUTO_INCREMENT,
    draftpad_id INT             NOT NULL,
    embedding   VECTOR(1024)    DEFAULT NULL,
    model_name  VARCHAR(100)    DEFAULT '',
    chunk_index INT             DEFAULT 0,
    created_at  TIMESTAMP(6)    NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    INDEX idx_dv_draftpad (draftpad_id),
    INDEX idx_dv_model (model_name),
    CONSTRAINT fk_dv_draftpad
        FOREIGN KEY (draftpad_id) REFERENCES draftpad(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;
"""

DDL_AUDIT_LOG = """
CREATE TABLE IF NOT EXISTS draftpad_audit (
    id          INT             NOT NULL AUTO_INCREMENT,
    session_id  VARCHAR(36)     NOT NULL,
    record_id   INT             NOT NULL,
    operation   VARCHAR(16)     NOT NULL COMMENT 'DELETE | UPDATE',
    old_content TEXT            DEFAULT NULL,
    new_content TEXT            DEFAULT NULL,
    created_at  TIMESTAMP(6)    NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    INDEX idx_audit_session (session_id),
    INDEX idx_audit_record (record_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;
"""


# ──────────────────────────────────────────────
# MariaDB 连接器
# ──────────────────────────────────────────────

class Database:
    """
    MariaDB 连接管理器。

    职责：
      - 连接池初始化
      - DDL 自动建表 + 迁移
      - CRUD + UPSERT 操作
      - jieba 分词（CJK FULLTEXT 支持）
      - 审计日志记录
    """

    def __init__(self, config: dict) -> None:
        self._config = config
        self._pool: Optional[mariadb.ConnectionPool] = None
        # 运行时检测 content_tokenized 列是否可用
        self._has_tokenized_column: Optional[bool] = None

    # ── 连接管理 ──

    def connect(self) -> None:
        """初始化连接池，自动建表 + 迁移。"""
        db_conf = self._config.get("mariadb", {})
        pool_conf = {
            "host": db_conf.get("host", "localhost"),
            "port": db_conf.get("port", 3306),
            "database": db_conf.get("database", "melquiades_medicamento"),
            "user": db_conf.get("user", "melquiades_user"),
            "password": db_conf.get("password", ""),
            "pool_name": db_conf.get("pool_name", "melquiades_pool"),
            "pool_size": db_conf.get("pool_size", 5),
            "connect_timeout": db_conf.get("connect_timeout", 10),
            "autocommit": True,
        }
        logger.info(
            "Connecting to MariaDB: %s@%s:%d/%s",
            pool_conf["user"], pool_conf["host"],
            pool_conf["port"], pool_conf["database"],
        )
        self._pool = mariadb.ConnectionPool(**pool_conf)
        self._ensure_tables()
        self._migrate_tokenized()
        logger.info("Database pool ready (%d connections)", pool_conf["pool_size"])

    def close(self) -> None:
        """关闭连接池。"""
        self._pool = None
        self._has_tokenized_column = None
        logger.info("Database pool closed")

    def _get_conn(self) -> mariadb.Connection:
        """从连接池取一个连接。"""
        if self._pool is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._pool.get_connection()

    def _ensure_tables(self) -> None:
        """自动建表（幂等）。"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(DDL_DRAFTPAD)
            cur.execute(DDL_DRAFTPAD_VECTORS)
            cur.execute(DDL_AUDIT_LOG)
            conn.commit()
            logger.info("Tables ensured (draftpad, draftpad_vectors, draftpad_audit)")
        finally:
            conn.close()

    # ── 迁移 ──

    def _migrate_tokenized(self) -> None:
        """
        为已有 draftpad 表添加 content_tokenized 列和 FULLTEXT 索引。

        MariaDB 10.2+ 支持 ADD COLUMN IF NOT EXISTS 和
        ADD FULLTEXT INDEX IF NOT EXISTS，幂等执行。
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            try:
                cur.execute(DDL_MIGRATE_TOKENIZED_COLUMN)
                conn.commit()
                logger.info("Migration: content_tokenized column ensured")
            except Exception as exc:
                logger.debug("Migration column skip (may already exist): %s", exc)

            try:
                cur.execute(DDL_MIGRATE_TOKENIZED_INDEX)
                conn.commit()
                logger.info("Migration: ft_draftpad_tokenized index ensured")
            except Exception as exc:
                logger.debug("Migration index skip (may already exist): %s", exc)

            self._has_tokenized_column = True
        finally:
            conn.close()

    # ── CJK 分词（写入 content_tokenized 列）──

    @staticmethod
    def _tokenize_cjk(text: str) -> str:
        """把文本切成 CJK unigram+bigram 串（写入 content_tokenized）。

        ★ 2026-10-25 口径切换：原实现是 jieba 分词。实测两个致命问题：
          1. jieba 切出的 2 字词在 `innodb_ft_min_token_size=3` 时代被索引全部
             丢弃（`+口径*` = 0 命中），当时只能弃用本列、改走 `content` 列，
             于是**放弃了分词检索能力**；
          2. 即便把该参数改为 1 并重建索引，仍有约 30% 漏搜——jieba 的切分
             边界与用户查询的词边界不一致（"会话" 被切成 "会 话"）。
        改用机械切分（Lucene CJKBigramFilter / ES cjk_bigram 的同一方案）：
        每个汉字一个 token + 每对相邻汉字一个 token，不依赖词典，任意子串可搜。

        前置条件：MariaDB `innodb_ft_min_token_size = 1`（已配）。
        查询侧对应 `tokenizer.build_cjk_boolean`，两侧必须同一口径。
        """
        from melquiades.tokenizer import tokenize_cjk_ub
        return tokenize_cjk_ub(text or "")

    # ── CRUD: 写操作 ──

    def insert_note(self, note: DraftpadNote) -> int:
        """
        插入一条笔记。

        返回: 新记录的自增 ID。
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            tokenized = self._tokenize_cjk(note.content)
            cur.execute(
                "INSERT INTO draftpad "
                "(session_id, note_key, content, content_tokenized, tags, model_name, chunk_index) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    note.session_id,
                    note.note_key[:NOTE_KEY_LENGTH],
                    note.content[:MAX_CONTENT_LENGTH],
                    tokenized[:MAX_CONTENT_LENGTH],
                    note.tag_string(),
                    note.model_name[:MODEL_NAME_LENGTH],
                    note.chunk_index,
                ),
            )
            conn.commit()
            new_id = cur.lastrowid
            logger.debug("Inserted draftpad id=%d key='%s' (session=%s)",
                         new_id, note.note_key, note.session_id)
            return new_id
        finally:
            conn.close()

    def upsert_by_key(
        self,
        session_id: str,
        note_key: str,
        content: str,
        tags: list[str],
        model_name: str = "",
    ) -> int:
        """
        按 note_key UPSERT（INSERT 或 UPDATE）。

        返回: 影响记录的 ID。
        设计: note_key + session_id 联合唯一 → 精确锁定。
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor(dictionary=True)

            # 1. 查找已有记录
            cur.execute(
                "SELECT id, content FROM draftpad "
                "WHERE session_id=? AND note_key=? AND deleted_at IS NULL",
                (session_id, note_key[:NOTE_KEY_LENGTH]),
            )
            row = cur.fetchone()

            tag_json = DraftpadNote(tags=tags).tag_string()
            truncated = content[:MAX_CONTENT_LENGTH]
            tokenized = self._tokenize_cjk(content)[:MAX_CONTENT_LENGTH]

            if row:
                # UPDATE
                note_id = row["id"]
                old_content = row["content"]

                cur.execute(
                    "UPDATE draftpad SET content=?, content_tokenized=?, tags=?, model_name=? "
                    "WHERE id=? AND deleted_at IS NULL",
                    (truncated, tokenized, tag_json, model_name[:MODEL_NAME_LENGTH], note_id),
                )

                # 审计日志
                cur.execute(
                    "INSERT INTO draftpad_audit "
                    "(session_id, record_id, operation, old_content, new_content) "
                    "VALUES (?, ?, 'UPDATE', ?, ?)",
                    (session_id, note_id, old_content, truncated),
                )

                conn.commit()
                logger.debug("Upsert (UPDATE) draftpad id=%d key='%s'", note_id, note_key)
                return note_id
            else:
                # INSERT
                cur.execute(
                    "INSERT INTO draftpad "
                    "(session_id, note_key, content, content_tokenized, tags, model_name) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, note_key[:NOTE_KEY_LENGTH],
                     truncated, tokenized, tag_json, model_name[:MODEL_NAME_LENGTH]),
                )
                conn.commit()
                new_id = cur.lastrowid
                logger.debug("Upsert (INSERT) draftpad id=%d key='%s'", new_id, note_key)
                return new_id
        finally:
            conn.close()

    def update_note(self, note_id: int, content: str, tags: list[str]) -> bool:
        """
        按 ID 更新笔记内容（同时记录旧内容到审计日志）。

        返回: True 如果找到并更新。
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor(dictionary=True)

            cur.execute(
                "SELECT session_id, content FROM draftpad WHERE id=? AND deleted_at IS NULL",
                (note_id,),
            )
            row = cur.fetchone()
            if row is None:
                logger.warning("update_note: id=%d not found or deleted", note_id)
                return False

            old_content = row["content"]
            session_id = row["session_id"]

            tag_json = DraftpadNote(tags=tags).tag_string()
            truncated = content[:MAX_CONTENT_LENGTH]
            tokenized = self._tokenize_cjk(content)[:MAX_CONTENT_LENGTH]

            cur.execute(
                "UPDATE draftpad SET content=?, content_tokenized=?, tags=? "
                "WHERE id=? AND deleted_at IS NULL",
                (truncated, tokenized, tag_json, note_id),
            )

            cur.execute(
                "INSERT INTO draftpad_audit "
                "(session_id, record_id, operation, old_content, new_content) "
                "VALUES (?, ?, 'UPDATE', ?, ?)",
                (session_id, note_id, old_content, truncated),
            )

            conn.commit()
            logger.debug("Updated draftpad id=%d", note_id)
            return True
        finally:
            conn.close()

    def soft_delete(self, note_id: int) -> bool:
        """
        软删除笔记（标记 deleted_at）。

        返回: True 如果找到并删除。
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor(dictionary=True)

            cur.execute(
                "SELECT session_id, content FROM draftpad WHERE id=? AND deleted_at IS NULL",
                (note_id,),
            )
            row = cur.fetchone()
            if row is None:
                logger.warning("soft_delete: id=%d not found or deleted", note_id)
                return False

            cur.execute(
                "UPDATE draftpad SET deleted_at=CURRENT_TIMESTAMP(6) WHERE id=?",
                (note_id,),
            )

            cur.execute(
                "INSERT INTO draftpad_audit "
                "(session_id, record_id, operation, old_content) "
                "VALUES (?, ?, 'DELETE', ?)",
                (row["session_id"], note_id, row["content"]),
            )

            conn.commit()
            logger.debug("Soft-deleted draftpad id=%d", note_id)
            return True
        finally:
            conn.close()

    # ── CRUD: 读操作 ──

    def get_note(self, note_id: int) -> Optional[DraftpadNote]:
        """按 ID 查询笔记（未删除的）。"""
        conn = self._get_conn()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT id, session_id, note_key, content, tags, "
                "model_name, chunk_index, created_at, updated_at, deleted_at "
                "FROM draftpad WHERE id=? AND deleted_at IS NULL",
                (note_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return self._row_to_note(row)
        finally:
            conn.close()

    def get_note_by_key(self, session_id: str, note_key: str) -> Optional[DraftpadNote]:
        """按 session_id + note_key 查询笔记。"""
        conn = self._get_conn()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT id, session_id, note_key, content, tags, "
                "model_name, chunk_index, created_at, updated_at, deleted_at "
                "FROM draftpad WHERE session_id=? AND note_key=? AND deleted_at IS NULL",
                (session_id, note_key[:NOTE_KEY_LENGTH]),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return self._row_to_note(row)
        finally:
            conn.close()

    def get_session_notes(
        self, session_id: str, limit: int = 50
    ) -> list[DraftpadNote]:
        """查询某会话的所有未删除笔记（按时间降序）。"""
        conn = self._get_conn()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT id, session_id, note_key, content, tags, "
                "model_name, chunk_index, created_at, updated_at, deleted_at "
                "FROM draftpad WHERE session_id=? AND deleted_at IS NULL "
                "ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            )
            return [self._row_to_note(row) for row in cur.fetchall()]
        finally:
            conn.close()

    # ── 全文搜索 ──

    def fulltext_search(
        self,
        query: str,
        session_id: str,
        top_k: int = 5,
    ) -> list[DraftpadNote]:
        """
        MariaDB FULLTEXT 搜索（BOOLEAN MODE）。

        CJK 自适应：
          - 查询包含中文/日文/韩文 → 对 query 做 jieba 分词，
            搜索 content_tokenized 列。
          - 纯英文查询 → 搜索 content 列（原有行为）。

        jieba 未安装时，CJK 查询回退到 content 列搜索
        （效果差但不会报错，靠调用侧 B 方案的向量权重弥补）。
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor(dictionary=True)

            # 判断是否使用分词列
            use_tokenized = _has_cjk(query) and jieba is not None

            if use_tokenized:
                search_col = "content_tokenized"
                # 对 query 分词后构建布尔查询
                tokenized_query = self._tokenize_cjk(query)
                words = tokenized_query.split()
                logger.debug("CJK FTS: using content_tokenized, query tokens=%s", words)
            else:
                search_col = "content"
                words = query.split()

            boolean_query = " ".join(
                f"+{word}*" if len(word) > 1 else f"+{word}"
                for word in words
            )

            sql = (
                f"SELECT id, session_id, note_key, content, tags, "
                f"model_name, chunk_index, created_at, updated_at, deleted_at, "
                f"MATCH({search_col}) AGAINST(? IN BOOLEAN MODE) AS score "
                f"FROM draftpad WHERE session_id=? AND deleted_at IS NULL "
                f"AND MATCH({search_col}) AGAINST(? IN BOOLEAN MODE) "
                f"ORDER BY score DESC LIMIT ?"
            )

            cur.execute(sql, (boolean_query, session_id, boolean_query, top_k))
            return [self._row_to_note(row) for row in cur.fetchall()]
        finally:
            conn.close()

    # ── 审计日志 ──

    def get_audit_log(
        self,
        session_id: str,
        since: Optional[str] = None,
        limit: int = 50,
    ) -> list[AuditEntry]:
        """查询会话的审计日志。"""
        conn = self._get_conn()
        try:
            cur = conn.cursor(dictionary=True)
            if since:
                cur.execute(
                    "SELECT id, session_id, record_id, operation, "
                    "old_content, new_content, created_at "
                    "FROM draftpad_audit "
                    "WHERE session_id=? AND created_at >= ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (session_id, since, limit),
                )
            else:
                cur.execute(
                    "SELECT id, session_id, record_id, operation, "
                    "old_content, new_content, created_at "
                    "FROM draftpad_audit "
                    "WHERE session_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (session_id, limit),
                )
            return [AuditEntry(**row) for row in cur.fetchall()]
        finally:
            conn.close()

    # ── 辅助 ──

    @staticmethod
    def _row_to_note(row: dict) -> DraftpadNote:
        """将数据库行转换为 DraftpadNote 对象。"""
        return DraftpadNote(
            id=row["id"],
            session_id=row["session_id"],
            note_key=row.get("note_key", ""),
            content=row["content"],
            tags=DraftpadNote.from_tag_string(row.get("tags", "[]")),
            model_name=row.get("model_name", ""),
            chunk_index=row.get("chunk_index", 0),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            deleted_at=row.get("deleted_at"),
        )
