"""
文件名: melquiades/backfill.py
摘要: 历史数据回填 —— 让两条检索路从"占位"变成"可用"。

  ① content_tokenized  — jieba 分词（供 CJK FULLTEXT 检索）
  ② embedding          — BGE 向量（供语义检索）

两者都**幂等**（只处理缺失行），可重复跑、可断点续跑。

纪律:
  · 批量处理 + 进度输出
  · **失败不中断**（记录并继续），最后汇报成功/失败计数（铁律四）
  · 全程即弃连接（铁律一）

依赖: dbcore.py, tokenizer.py, retrieval.py（取向量）
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from melquiades.dbcore import MariaDB
from melquiades.schema import ZERO_VECTOR
from melquiades.tokenizer import tokenize

logger = logging.getLogger(__name__)

ProgressFn = Optional[Callable[[int, int, str], None]]


def _default_progress(done: int, total: int, note: str) -> None:
    if total:
        pct = done * 100 // total
        print(f"  [{done}/{total}] {pct}% {note}", flush=True)


def count_missing_tokenized(db: MariaDB, table: str = "draftpad") -> int:
    """统计缺 content_tokenized 的行数。"""
    return int(db.scalar(
        f"SELECT COUNT(*) FROM {table} "
        "WHERE content_tokenized IS NULL OR content_tokenized = ''"
    ) or 0)


def backfill_tokenized(
    db: MariaDB,
    table: str = "draftpad",
    limit: int = 0,
    progress: ProgressFn = None,
) -> dict[str, int]:
    """
    回填 content_tokenized（jieba 分词）。

    参数:
      limit — 最多处理多少行（0 = 全部）

    返回: {"total": 待处理数, "ok": 成功数, "failed": 失败数}
    """
    emit = progress or _default_progress
    sql_where = "WHERE content_tokenized IS NULL OR content_tokenized = ''"
    total = int(db.scalar(f"SELECT COUNT(*) FROM {table} {sql_where}") or 0)
    if limit and total > limit:
        total = limit

    ok = failed = 0
    cur = db.cursor(dictionary=True)
    try:
        with cur as c:
            q = (f"SELECT id, content FROM {table} {sql_where} ORDER BY id"
                 + (f" LIMIT {int(limit)}" if limit else ""))
            c.execute(q)
            rows = c.fetchall() or []
    except Exception as exc:
        logger.error("backfill_tokenized query failed: %s", exc)
        return {"total": 0, "ok": 0, "failed": 0}

    for i, row in enumerate(rows, 1):
        try:
            tok = tokenize(row.get("content") or "")
            db.execute(
                f"UPDATE {table} SET content_tokenized=? WHERE id=?",
                (tok, row["id"]),
            )
            ok += 1
        except Exception as exc:
            failed += 1
            logger.warning("tokenize row id=%s failed: %s", row.get("id"), exc)
        if i % 50 == 0 or i == len(rows):
            emit(i, len(rows), f"tokenized ok={ok} failed={failed}")

    return {"total": len(rows), "ok": ok, "failed": failed}


def count_missing_embedding(db: MariaDB, table: str = "draftpad") -> int:
    """统计缺向量的行数（以 embedded 标记列为准 —— VECTOR 列比较不可靠）。"""
    return int(db.scalar(
        f"SELECT COUNT(*) FROM {table} WHERE embedded = 0",
    ) or 0)


def backfill_embeddings(
    db: MariaDB,
    embed_fn: Callable[[str], Optional[list[float]]],
    table: str = "draftpad",
    text_col: str = "content",
    limit: int = 0,
    progress: ProgressFn = None,
) -> dict[str, int]:
    """
    回填 embedding（BGE 向量）。

    参数:
      embed_fn — 取向量的可调用（通常 Retriever.embed，内含多端点回退）
      limit    — 最多处理多少行（0 = 全部）

    返回: {"total", "ok", "failed", "skipped"}
    """
    import json as _json

    emit = progress or _default_progress
    # dsh_compaction 的正文列是 summary，没有 content 列
    if text_col == "content" and table == "dsh_compaction":
        text_col = "summary"
    rows = db.query(
        f"SELECT id, LEFT({text_col}, 6000) AS txt FROM {table} "
        "WHERE embedded = 0 ORDER BY id"
        + (f" LIMIT {int(limit)}" if limit else ""),
    )

    ok = failed = 0
    for i, row in enumerate(rows, 1):
        text = (row.get("txt") or "").strip()
        if not text:
            failed += 1
            continue
        try:
            emb = embed_fn(text)
            if emb is None:
                failed += 1
                logger.warning("embed returned None for id=%s", row.get("id"))
            elif len(set(emb)) == 1 and emb[0] == 0.0:
                failed += 1
                logger.warning("embed returned zero vector for id=%s", row.get("id"))
            else:
                db.execute(
                    f"UPDATE {table} SET embedding=VEC_FromText(?), embedded=1 "
                    "WHERE id=?",
                    (_json.dumps(emb), row["id"]),
                )
                ok += 1
        except Exception as exc:
            failed += 1
            logger.warning("embed row id=%s failed: %s", row.get("id"), exc)
        if i % 20 == 0 or i == len(rows):
            emit(i, len(rows), f"embedded ok={ok} failed={failed}")
        # 连续失败过多则提前退出（多半是服务挂了，别把库空转一遍）
        if failed >= 20 and ok == 0:
            logger.error("embedding backfill aborted: 20 consecutive failures at start")
            break

    return {"total": len(rows), "ok": ok, "failed": failed}
