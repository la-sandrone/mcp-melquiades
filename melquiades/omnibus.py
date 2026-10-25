"""
文件名: melquiades/omnibus.py
摘要: 跨源统一检索 —— 一次查询同时搜「草稿纸 / 会话正文 / 压缩摘要」，
      每个源按其实际能力走 向量 + FULLTEXT + LIKE 三路，归一化后融合排序。

为什么需要它（2026-10-25 用户提出）：
  写长篇小说/设定集的工作区里，「找一句以前写过的东西」天然跨三处：
    · 草稿纸（draftpad）      —— 人写的结论与钦定口径
    · 会话正文（dsh_message） —— 当时怎么讨论出来的，含工具输出
    · 压缩摘要（dsh_compaction）—— 被压掉的上文
  现有的 draftpad_find / archive_find 各自只搜一个源，得手动跑三遍再人工合并。
  search_OMNIA 把它们合成一次调用，但**不替代**它们——精细模式（vector /
  fulltext / clue / keyword / condition / rerank / expand）仍然在那两个工具里。

能力矩阵（决定了三路覆盖到哪）：
  | 源            | 向量 | FULLTEXT | LIKE |
  |---------------|------|----------|------|
  | draftpad      | ✅   | ✅ bigram | ✅   |
  | dsh_message   | ❌   | ✅ bigram | ✅   |
  | dsh_compaction| ✅   | ✅ bigram | ✅   |
  dsh_message 没有 embedding 列：给它补向量是 14 万行的大工程，暂不做。
  ⇒ 语义（模糊）召回由草稿纸与压缩摘要承担；会话正文靠精确路（bigram 分词
    已支持单字/任意子串）与 LIKE 兜底。工具描述里必须如实说明这一点。

依赖: melquiades.retrieval(Retriever.embed)、melquiades.tokenizer(build_cjk_boolean/escape_like)、
      melquiades.db(MariaDB 封装)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from melquiades.tokenizer import build_cjk_boolean, escape_like, has_cjk

logger = logging.getLogger(__name__)

# ── 各源的形状与能力 ────────────────────────────────────────────────
# select: 需要带回的列；vec=是否可用向量路；text=原文列；tok=分词列
SOURCE_SPECS: dict[str, dict[str, Any]] = {
    "draftpad": {
        "label": "草稿纸",
        "table": "draftpad",
        "id_col": "id",
        "text_col": "content",
        "tok_col": "content_tokenized",
        "vec": True,
        "select": "id, session_id, note_key, content, tags, created_at",
        "where": "deleted_at IS NULL",
    },
    "messages": {
        "label": "会话正文",
        "table": "dsh_message",
        "id_col": "guid",
        "text_col": "body",
        "tok_col": "body_tokenized",
        "vec": False,
        "select": "guid, session_id, ev_type, kind, role, ev_time, body",
        "where": "body IS NOT NULL",
    },
    "compactions": {
        "label": "压缩摘要",
        "table": "dsh_compaction",
        "id_col": "id",
        "text_col": "summary",
        "tok_col": "content_tokenized",
        "vec": True,
        "select": "id, dsh_session, compaction_id, turn_no, occurred_at, summary, shadowed_count",
        "where": "1=1",
    },
}

DEFAULT_SOURCES = ("draftpad", "messages", "compactions")
DEFAULT_MODES = ("vector", "fulltext", "like")

# 融合权重：向量偏"像"，FULLTEXT 偏"准"，LIKE 是字面兜底。
# 同一条被多路命中时额外加成（每多一路 +0.08），避免只靠单路的条目压倒多路共识。
WEIGHTS = {"vector": 0.45, "fulltext": 0.40, "like": 0.15}
MULTI_ROUTE_BONUS = 0.08


@dataclass
class OmnibusHit:
    source: str
    record_id: str
    score: float = 0.0
    matched_by: list[str] = field(default_factory=list)
    snippet: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def _clip(text: str, query: str, width: int = 240) -> str:
    """取命中附近的片段：能定位 query 就居中，否则取开头。"""
    if not text:
        return ""
    flat = " ".join(text.split())
    if not query:
        return flat[:width]
    idx = flat.lower().find(query.lower())
    if idx < 0:
        return flat[:width] + ("…" if len(flat) > width else "")
    lo = max(0, idx - width // 3)
    hi = min(len(flat), lo + width)
    return ("…" if lo > 0 else "") + flat[lo:hi] + ("…" if hi < len(flat) else "")


def _row_meta(source: str, row: dict[str, Any]) -> dict[str, Any]:
    if source == "draftpad":
        return {
            "note_key": row.get("note_key") or "",
            "created_at": str(row.get("created_at") or ""),
            "tags": row.get("tags") or "",
            "session_id": row.get("session_id") or "",
        }
    if source == "messages":
        return {
            "session_id": row.get("session_id") or "",
            "ev_type": row.get("ev_type") or "",
            "kind": row.get("kind") or "",
            "role": row.get("role") or "",
            "ev_time": str(row.get("ev_time") or ""),
        }
    return {
        "dsh_session": row.get("dsh_session") or "",
        "compaction_id": row.get("compaction_id") or "",
        "turn_no": row.get("turn_no"),
        "occurred_at": str(row.get("occurred_at") or ""),
        "shadowed_count": row.get("shadowed_count"),
    }


class OmnibusSearcher:
    """跨源三路检索器。持有 Retriever（复用其 embed 与 DB 连接）。"""

    def __init__(self, retriever) -> None:
        self._r = retriever

    # ── 单源单路 ───────────────────────────────────────────────

    def _vec(self, source: str, emb: list[float], limit: int) -> list[OmnibusHit]:
        # **⚠️ 这是近似检索（ANN），结果不保证精确**
        #
        # MariaDB 的 `VECTOR KEY` 是 HNSW —— 近似最近邻，recall 可以低于 100%，
        # 而且它不会告诉你漏了什么。2026-10-25 在 memoria 的 documents_book_chunk
        # （62.5 万点）上用自查询实测：
        #
        #     自查询 top-1 命中自己： 12/13 = 92%
        #     自己在 top-10 里：      13/13 = 100%
        #     （mhnsw_ef_search=20；调到 400 无任何变化，该参数在 12.3.2 上未见效）
        #
        # 排除过程：IGNORE INDEX 暴力扫描 → 精确正确；Python 手算余弦 → 0.000000；
        # EXPLAIN → 确实走 idx_vec_chunk。所以数据与距离函数都对，是索引给了近似答案。
        #
        # 两条纪律：
        #   1. 可以用它做语义召回（top-k 足够，实测 top-10 100%）——本函数正是此用途；
        #   2. 绝不要用它做精确查找（自查询/判重/找重复/验证是否已入库），那会撞上那 8%。
        #      精确查找走 FULLTEXT 或 LIKE。
        #
        # 表越小 recall 越高（图小、连通性好），但不要假设小表就是 100%。
        # 另注：VEC_DISTANCE（不带 _COSINE）会报 index is not found；建索引时
        # DISTANCE=cosine 必须显式写，否则静默降级为 mhnsw_default_distance（euclidean）。

        spec = SOURCE_SPECS[source]
        if not spec["vec"]:
            return []
        sql = (
            f"SELECT {spec['select']}, VEC_DISTANCE_COSINE(embedding, q.qv) AS _dist "
            f"FROM {spec['table']}, (SELECT VEC_FromText(?) AS qv) q "
            f"WHERE embedded = 1 AND ({spec['where']}) "
            f"ORDER BY _dist ASC LIMIT ?"
        )
        try:
            rows = self._r._db.query(sql, (json.dumps(emb), limit), readonly=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("omnibus vector failed (%s): %s", source, exc)
            return []
        out = []
        for row in rows:
            dist = float(row.get("_dist") or 0.0)
            h = OmnibusHit(
                source=source,
                record_id=str(row.get(spec["id_col"])),
                score=1.0 / (1.0 + dist),
                matched_by=["vector"],
                meta=_row_meta(source, row),
            )
            h.snippet = _clip(str(row.get(spec["text_col"]) or ""), "")
            out.append(h)
        return out

    def _fulltext(self, source: str, query: str, limit: int) -> list[OmnibusHit]:
        spec = SOURCE_SPECS[source]
        bo = build_cjk_boolean(query) if has_cjk(query) else " ".join(
            f"+{w}" for w in query.split() if w
        )
        if not bo:
            return []
        sql = (
            f"SELECT {spec['select']}, MATCH({spec['tok_col']}) AGAINST(? IN BOOLEAN MODE) AS _s "
            f"FROM {spec['table']} "
            f"WHERE MATCH({spec['tok_col']}) AGAINST(? IN BOOLEAN MODE) AND ({spec['where']}) "
            f"ORDER BY _s DESC LIMIT ?"
        )
        try:
            rows = self._r._db.query(sql, (bo, bo, limit), readonly=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("omnibus fulltext failed (%s): %s", source, exc)
            return []
        if not rows:
            return []
        top = max(float(r.get("_s") or 0.0) for r in rows) or 1.0
        out = []
        for row in rows:
            h = OmnibusHit(
                source=source,
                record_id=str(row.get(spec["id_col"])),
                score=float(row.get("_s") or 0.0) / top,
                matched_by=["fulltext"],
                meta=_row_meta(source, row),
            )
            h.snippet = _clip(str(row.get(spec["text_col"]) or ""), query)
            out.append(h)
        return out

    def _like(self, source: str, query: str, limit: int) -> list[OmnibusHit]:
        spec = SOURCE_SPECS[source]
        if not query.strip():
            return []
        sql = (
            f"SELECT {spec['select']} FROM {spec['table']} "
            f"WHERE {spec['text_col']} LIKE ? ESCAPE '\\\\' AND ({spec['where']}) "
            f"ORDER BY {spec['id_col']} DESC LIMIT ?"
        )
        try:
            rows = self._r._db.query(
                sql, (f"%{escape_like(query)}%", limit), readonly=True
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("omnibus like failed (%s): %s", source, exc)
            return []
        out = []
        for row in rows:
            h = OmnibusHit(
                source=source,
                record_id=str(row.get(spec["id_col"])),
                score=1.0,
                matched_by=["like"],
                meta=_row_meta(source, row),
            )
            h.snippet = _clip(str(row.get(spec["text_col"]) or ""), query)
            out.append(h)
        return out

    # ── 主入口 ─────────────────────────────────────────────────

    def search(
        self,
        query: str,
        limit: int = 5,
        sources: Optional[list[str]] = None,
        modes: Optional[list[str]] = None,
        per_source_limit: int = 0,
    ) -> dict[str, Any]:
        """三路 × 多源，归一化融合。

        ★ 取数口径（2026-10-25 用户拍板，别再改回"宽取后窄截"）：
          `limit` 是**每一路各取多少条**，不是"最终返回多少条"。
          三路各取 limit 条 → 按 (source, id) **去重** → 剩多少给多少，
          **不截断回 limit**。
          理由：宽取后窄截会把「只有某一路命中、但分数偏低」的独特结果挤掉，
          而这类结果恰恰是最不该漏的（例如只有 LIKE 命中的旧措辞）。
          代价是返回条数常多于 limit——这是有意的，不是 bug。

        返回 {"hits": [...], "stats": {...}, "notes": [...]}
        """
        query = (query or "").strip()
        srcs = [s for s in (sources or DEFAULT_SOURCES) if s in SOURCE_SPECS]
        mds = [m for m in (modes or DEFAULT_MODES) if m in WEIGHTS]
        notes: list[str] = []
        # 每路取多少：显式给了 per_source_limit 就用它，否则 = limit
        per_route_n = int(per_source_limit or limit)

        if not query:
            return {"hits": [], "stats": {}, "notes": ["查询串为空"]}

        emb = None
        if "vector" in mds and any(SOURCE_SPECS[s]["vec"] for s in srcs):
            emb = self._r.embed(query)
            if emb is None:
                notes.append("向量路不可用（BGE 全部端点失败），本次只走全文与 LIKE")

        per_route: dict[tuple[str, str], list[OmnibusHit]] = {}
        for src in srcs:
            if "vector" in mds and emb is not None:
                per_route[(src, "vector")] = self._vec(src, emb, per_route_n)
            if "fulltext" in mds:
                per_route[(src, "fulltext")] = self._fulltext(src, query, per_route_n)
            if "like" in mds:
                per_route[(src, "like")] = self._like(src, query, per_route_n)

        # 融合：同一 (source, id) 多路命中 ⇒ 加权求和 + 多路加成
        merged: dict[tuple[str, str], OmnibusHit] = {}
        for (src, mode), hits in per_route.items():
            w = WEIGHTS[mode]
            for h in hits:
                key = (h.source, h.record_id)
                cur = merged.get(key)
                if cur is None:
                    h.score = h.score * w
                    merged[key] = h
                else:
                    cur.score += h.score * w
                    if mode not in cur.matched_by:
                        cur.matched_by.append(mode)
                    if not cur.snippet:
                        cur.snippet = h.snippet

        for h in merged.values():
            h.score += MULTI_ROUTE_BONUS * (len(h.matched_by) - 1)

        # ★ 去重后全给，不截断回 limit（见方法 docstring 的取数口径）。
        #   硬上限只防"limit 被传得很大 + 三源三路"时的输出爆炸。
        ranked = sorted(merged.values(), key=lambda x: -x.score)
        hard_cap = max(limit * len(mds) * 3, 60)
        if len(ranked) > hard_cap:
            notes.append(f"去重后 {len(ranked)} 条，超出硬上限 {hard_cap}，已截断")
            ranked = ranked[:hard_cap]

        stats = {
            "sources": {s: len([h for h in ranked if h.source == s]) for s in srcs},
            "routes": {m: sum(len(v) for (s, mm), v in per_route.items() if mm == m) for m in mds},
            "vector_available": emb is not None,
            "per_route_limit": per_route_n,
            "returned_after_dedup": len(ranked),
        }
        if not SOURCE_SPECS["messages"]["vec"]:
            notes.append(
                "会话正文无向量列（未做 embedding），该源只走 全文(bigram) + LIKE；"
                "语义召回由草稿纸与压缩摘要承担"
            )
        return {"hits": ranked, "stats": stats, "notes": notes}


def format_hits(result: dict[str, Any]) -> str:
    """把结果格式化成 MCP 工具返回的文本。"""
    hits: list[OmnibusHit] = result["hits"]
    stats = result.get("stats") or {}
    notes = result.get("notes") or []

    if not hits:
        msg = ["未命中。"]
        if notes:
            msg += ["", "注：" + "；".join(notes)]
        return "\n".join(msg)

    label_of = {k: v["label"] for k, v in SOURCE_SPECS.items()}
    lines = [
        f"命中 {len(hits)} 条 | 按源: "
        + ", ".join(f"{label_of[s]}={n}" for s, n in stats.get("sources", {}).items())
        + f" | 各路原始命中: " + ", ".join(f"{k}={v}" for k, v in stats.get("routes", {}).items())
    ]
    if notes:
        lines.append("注：" + "；".join(notes))
    lines.append("")

    for i, h in enumerate(hits, 1):
        meta = h.meta
        tag = "/".join(h.matched_by)
        lines.append(f"[{i}] 「{label_of[h.source]}」score={h.score:.3f} 路由={tag}")
        if h.source == "messages":
            # ★ 必须给出**完整 guid**：调用方下一步就是 message_context(guid)，
            #   只显示 session_id 等于给了结果却接不上下一步（2026-10-25 实测踩到）。
            lines.append(
                f"    guid={h.record_id}"
                f" · 会话 {meta['session_id'][:20]}… · {meta['ev_type']}/{meta['kind']}"
                f" · {meta['ev_time']}"
            )
        elif h.source == "draftpad":
            lines.append(f"    笔记 #{h.record_id} · {meta.get('note_key','')[:40]} · {meta['created_at']}")
        else:
            lines.append(
                f"    压缩档案 #{h.record_id} · 会话 {meta['dsh_session'][:20]}…"
                f" · {meta['occurred_at']} · 遮蔽 {meta.get('shadowed_count')} 条"
            )
        lines.append("    " + (h.snippet or "(无正文)"))
        lines.append("")

    lines.append(
        "回溯指引: 草稿纸命中 → draftpad_find 精确模式复检（或按 id 取全文）；"
        "压缩摘要命中 → archive_expand(record_id) 取指针，再 archive_fetch 回查原文；"
        "会话正文命中 → 按 session_id + ev_time 定位到那次会话"
    )
    return "\n".join(lines)
