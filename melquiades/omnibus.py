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
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from melquiades.rerank import Reranker
from melquiades.session_meta import SessionMetaIndex, UNKNOWN_WORKSPACE
from melquiades.tokenizer import (build_cjk_boolean, build_cjk_query_v2,
                                  escape_like, has_cjk)

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
        # ★ 2026-11-07 打开：重建后 dsh_message 有了 VECTOR 索引 + 40,698 行真向量。
        #   实测（12 条金标查询，目标会话进前 20）：**关 4/12 → 开 11/12**，
        #   平均耗时 857ms → 839ms（基本不变）。
        #   这是「打捞不出来」的最后一环：查询侧改完、索引建好，但路没开等于白建。
        "vec": True,
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
DEFAULT_MODES = ("vector", "fulltext", "phrase", "like")

# 融合权重：向量偏"像"，FULLTEXT 偏"准"，LIKE 是字面兜底。
# 同一条被多路命中时额外加成（每多一路 +0.08），避免只靠单路的条目压倒多路共识。
# 短语路权重 0.90 —— 比向量路高一倍，因为它是**精确子串**证据（planet 级强信号）。
# 实测（25 个「词里含停词字」的词，precision@20，本库 194k 行）：
#   phrase=0.30 -> 84.0%  0.55 -> 91.6%  0.70 -> 93.0%  0.90 -> **96.6%**
#   不给短语路（vector+fulltext 两路）：36.2%（语义近邻把精确命中挤下去）
# 为什么可以这么高：短语只在 token 数 ≤ PHRASE_MAX_TOKENS 时才挂，命中的是**连续串**，
#   误命中靠 BM25 的 idf 与重排兜底；而这一类查询（生造词/标识符）要的就是精确。
WEIGHTS = {"vector": 0.45, "fulltext": 0.40, "phrase": 0.90, "like": 0.15}
MULTI_ROUTE_BONUS = 0.08


# LIKE 路的固定低分：它没有排序信号（见 _like 的 docstring），只作补充集。
# 0.05 远低于任一真实路的归一化分（0~1），保证它不会挤掉 BM25/向量命中。
LIKE_SUPPLEMENT_SCORE = 0.05

# 同工作区加权系数（DESIGN.md #15）。
# 为什么是「乘 1.25」而不是「加固定分」：分数是归一化到 0~1 的，乘性加权不改变
# 分数分布形状，也不会因为某条原本分低就永远追不上；加性会。
WORKSPACE_BOOST = 1.25

# 递补（#17）：去重后不足 limit 时，把每路取数放大这么多倍重跑一次。
# 为什么是乘性放大而不是逐个补：加深取数是**一次 SQL**，逐个补是 N 次。
DEEPEN_FACTOR = 4
MAX_PER_ROUTE = 200

# 短语路（档 1）的 token 上限：超过它就不挂这条路线。
# 理由：短语要求**逐 token 相邻**，整句问句基本恒为 0 命中，白付一次索引查询；
#   而真正的子串需求（生造词 / 标识符 / 路径片段）token 数总是很少。
PHRASE_MAX_TOKENS = 3


@dataclass
class OmnibusHit:
    source: str
    record_id: str
    score: float = 0.0
    matched_by: list[str] = field(default_factory=list)
    snippet: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    # 跨编码器分数（只有进过重排池的才有；None = 没排）
    rerank_score: Optional[float] = None


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


def _row_meta(source: str, row: dict[str, Any], meta_index=None) -> dict[str, Any]:
    if source == "draftpad":
        # 草稿纸也有 session_id（v2 起降级为 bucket），同样解析工作区/标题：
        # 「这条笔记是哪场对话里写的」是找回上下文的关键线索。
        sid = row.get("session_id") or ""
        title = ws_title = ws_path = ""
        if meta_index is not None and sid:
            try:
                m = meta_index.get(sid)
                title, ws_title, ws_path = m.title, m.workspace_title, m.workspace_path
            except Exception:  # noqa: BLE001
                pass
        return {
            "note_key": row.get("note_key") or "",
            "created_at": str(row.get("created_at") or ""),
            "tags": row.get("tags") or "",
            "session_id": sid,
            "session_title": title,
            "workspace_title": ws_title or (UNKNOWN_WORKSPACE if sid else ""),
            "workspace_path": ws_path,
        }
    if source == "messages":
        sid = row.get("session_id") or ""
        # ★ 把「session-xxxx」变成人能认的东西（DESIGN.md #24/#25）：
        #   工作区 + 会话标题。取不到就如实标「未分类工作区」，**绝不猜**。
        title = ""
        ws_title = ""
        ws_path = ""
        if meta_index is not None and sid:
            try:
                m = meta_index.get(sid)
                title, ws_title, ws_path = m.title, m.workspace_title, m.workspace_path
            except Exception:  # noqa: BLE001 —— 元信息失败不能拖垮检索
                pass
        return {
            "session_id": sid,
            "ev_type": row.get("ev_type") or "",
            "kind": row.get("kind") or "",
            "role": row.get("role") or "",
            "ev_time": str(row.get("ev_time") or ""),
            "session_title": title,
            "workspace_title": ws_title or (UNKNOWN_WORKSPACE if sid else ""),
            "workspace_path": ws_path,
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
        # 垂直分表的表名覆盖（DESIGN.md「下一版架构」）：空串 = 与主表同表。
        # 用**实例级副本**而不是改模块级字典 —— 改全局会让测试与多实例互相污染。
        self._specs = {k: dict(v) for k, v in SOURCE_SPECS.items()}
        sc = getattr(getattr(retriever, "_cfg", None), "search", None)
        if sc is not None:
            fts_t = str(getattr(sc, "messages_fts_table", "") or "")
            vec_t = str(getattr(sc, "messages_vec_table", "") or "")
            if fts_t:
                self._specs["messages"]["fts_table"] = fts_t
            if vec_t:
                self._specs["messages"]["vec_table"] = vec_t
                self._specs["messages"]["vec"] = True   # 侧表模式下会话正文有向量路

        # 跨编码器精排（DESIGN #31 档 3）。构造失败不能拖垮检索 —— 精排是加分项。
        self._rr = None
        try:
            self._rr = Reranker(sc) if sc is not None else None
        except Exception:  # noqa: BLE001
            self._rr = None

        # 会话元信息（工作区 / 标题）。构造时只**发现** home，真正加载在首次使用，
        # 且失败一律降级为空 —— 元信息挂了不能拖垮检索。
        cfg = getattr(retriever, "_cfg", None)
        try:
            self._meta = SessionMetaIndex.from_dsh_home(
                getattr(cfg, "dsh_home", None),
                getattr(getattr(cfg, "dsh", None), "harness_dir", None),
            )
        except Exception:  # noqa: BLE001
            self._meta = None

    # ── 单源单路 ───────────────────────────────────────────────

    # ── 取数口径：事件白名单 + inbox 档位（DESIGN.md #8/#9/#10）──────────

    def _cfg_search(self):
        cfg = getattr(self._r, "_cfg", None)
        return getattr(cfg, "search", None)

    def _stopwords(self) -> tuple:
        sc = self._cfg_search()
        return tuple(getattr(sc, "stopwords", ()) or ())

    def _source_extra(self, source: str) -> tuple[str, tuple]:
        """返回该源的附加 WHERE 片段与参数。目前只有 messages 有。

        ★ 白名单而非黑名单：DSH 升级可能新增控制面事件，黑名单会让新类型**默认自己漏进来**
          （症状沉默、延迟发作）。升级适配：与 known-event-types.ts 做差集。
        """
        if source != "messages":
            return "1=1", ()
        sc = self._cfg_search()
        evs = list(getattr(sc, "messages_ev_types", ()) or ())
        inbox = str(getattr(sc, "inbox", "off") or "off")
        if inbox == "only":
            evs = ["agent/inbox/spliced"]
        elif inbox == "both":
            evs = evs + ["agent/inbox/spliced"]
        if not evs:
            return "1=1", ()
        placeholders = ", ".join(["?"] * len(evs))
        return "ev_type IN (" + placeholders + ")", tuple(evs)

    def _hydrate(self, source: str, pairs: list, query: str) -> list[OmnibusHit]:
        """侧表模式：拿到 (id, score) 后回主表补 meta / snippet。

        ★ 白名单过滤必须在这里再做一次：侧表只存 guid + 那一列，没有 ev_type，
          所以「哪些事件算会话正文」只能在回主表时判。
          被过滤掉的不算孤儿，是**正常过滤**；主表真没有的才是孤儿（要告警）。
        """
        spec = self._specs[source]
        if not pairs:
            return []
        ids = [str(p_[0]) for p_ in pairs]
        ph = ", ".join(["?"] * len(ids))
        extra_sql, extra_params = self._source_extra(source)
        sql = ("SELECT " + spec["select"] + " FROM " + spec["table"] +
               " WHERE " + spec["id_col"] + " IN (" + ph + ") AND (" + extra_sql + ")")
        rows = self._r._db.query(sql, tuple(ids) + extra_params, readonly=True)
        by_id = {str(r.get(spec["id_col"])): r for r in rows}
        out = []
        missing = 0
        for rid, score, mode in pairs:
            row = by_id.get(str(rid))
            if row is None:
                missing += 1
                continue
            h = OmnibusHit(source=source, record_id=str(rid), score=score,
                           matched_by=[mode], meta=_row_meta(source, row, self._meta))
            h.snippet = _clip(str(row.get(spec["text_col"]) or ""), query)
            out.append(h)
        if missing:
            logger.debug("omnibus %s: 侧表命中 %d 条在主表不可见（白名单/已删）",
                         source, missing)
        return out


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

        spec = self._specs[source]
        if not spec["vec"]:
            return []

        # ★ 侧表模式（DESIGN.md「下一版架构」）：向量列搬到窄表，
        #   主表不再背 VECTOR 索引 —— 这样主表上的 DDL 恢复 INPLACE/INSTANT。
        #   侧表只存 embedded=1 的行，天然避开「17 万条零向量污染 HNSW」。
        vec_table = spec.get("vec_table")
        if vec_table:
            qv = json.dumps(emb)
            sql = ("SELECT guid, VEC_DISTANCE_COSINE(v, VEC_FromText(?)) AS _dist "
                   "FROM " + vec_table + " "
                   "ORDER BY VEC_DISTANCE_COSINE(v, VEC_FromText(?)) ASC LIMIT ?")
            rows = self._r._db.query(sql, (qv, qv, limit), readonly=True)
            pairs = [(str(r["guid"]), 1.0 / (1.0 + float(r.get("_dist") or 0.0)), "vector")
                     for r in rows]
            return self._hydrate(source, pairs, "")
        sql = (
            f"SELECT {spec['select']}, VEC_DISTANCE_COSINE(embedding, q.qv) AS _dist "
            f"FROM {spec['table']}, (SELECT VEC_FromText(?) AS qv) q "
            f"WHERE embedded = 1 AND ({spec['where']}) "
            f"ORDER BY _dist ASC LIMIT ?"
        )
        # ★ 不吞异常：让 search() 记进 degraded_routes（DESIGN.md #16）。
        #   吞掉的表现是「0 命中 + ok:true」—— 比崩溃恶劣得多。
        rows = self._r._db.query(sql, (json.dumps(emb), limit), readonly=True)
        out = []
        for row in rows:
            dist = float(row.get("_dist") or 0.0)
            h = OmnibusHit(
                source=source,
                record_id=str(row.get(spec["id_col"])),
                score=1.0 / (1.0 + dist),
                matched_by=["vector"],
                meta=_row_meta(source, row, self._meta),
            )
            h.snippet = _clip(str(row.get(spec["text_col"]) or ""), "")
            out.append(h)
        return out

    def _fulltext(self, source: str, query: str, limit: int,
                  diag: Optional[dict] = None) -> list[OmnibusHit]:
        """全文路。

        ★ 2026-11-07：**messages 源改走 NATURAL LANGUAGE MODE**（DESIGN.md #11 修订）。
          12 条查询实测：目标会话进前 50 从 3/8 → **12/12**，中位排名 4.5，61 ms。
          机制：NL 模式由引擎按 BM25 排序（InnoDB 全文仿 Sphinx），OR 语义不设硬 AND；
          我们只负责**切块 + 丢 unigram**，不再替引擎拼布尔式。
          ⚠️ **禁止与 AND 路做任何融合**（含 RRF）：实测等权/RRF/加成三种都把中位排名
             从 4.5 拉到 7~8 —— AND 路的行基本是 NL 路的子集，给子集加固定分等于降级。
          ⚠️ draftpad / compactions 的 tokenized 是**另一套口径**，保持 BOOLEAN 不动。
        """
        spec = self._specs[source]
        extra_sql, extra_params = self._source_extra(source)
        raw_expr = ""
        if source == "messages":
            expr = build_cjk_query_v2(query, self._stopwords())
            # ★ 未剪表达式（A4）：不套停词 —— 剪枝只许削弱精度，绝不许影响「能不能捞到」
            raw_expr = build_cjk_query_v2(query, ())
            mode = "NATURAL LANGUAGE MODE"
            if diag is not None:
                kept = expr.split()
                pruned = [t for t in raw_expr.split() if t not in set(kept)]
                diag.update({"query_expr": expr, "kept": kept, "pruned": pruned,
                             "retried_unpruned": False})
                # A3：剪枝率异常要喊出来 —— 这次误伤的全部损失原本都是静默的
                if pruned and len(pruned) >= len(kept):
                    logger.warning(
                        "stopword pruning removed %d of %d query terms: kept=%r pruned=%r",
                        len(pruned), len(pruned) + len(kept), kept, pruned[:8])
        else:
            expr = build_cjk_boolean(query) if has_cjk(query) else " ".join(
                f"+{w}" for w in query.split() if w
            )
            mode = "BOOLEAN MODE"
        if not expr:
            return []

        # ★ 侧表模式：tokenized 与 FULLTEXT 索引在窄表上，MATCH 在那边做，
        #   再回主表补 meta/snippet。白名单过滤在 _hydrate 里做。
        fts_table = spec.get("fts_table")

        def _rows(ex: str) -> list[dict[str, Any]]:
            if fts_table:
                col = spec["tok_col"]
                sql = ("SELECT guid, MATCH(" + col + ") AGAINST(? IN " + mode + ") AS _s "
                       "FROM " + fts_table + " "
                       "WHERE MATCH(" + col + ") AGAINST(? IN " + mode + ") "
                       "ORDER BY _s DESC LIMIT ?")
                return self._r._db.query(sql, (ex, ex, limit), readonly=True)
            sql = (
                f"SELECT {spec['select']}, MATCH({spec['tok_col']}) AGAINST(? IN {mode}) AS _s "
                f"FROM {spec['table']} "
                f"WHERE MATCH({spec['tok_col']}) AGAINST(? IN {mode}) AND ({spec['where']}) "
                f"AND ({extra_sql}) ORDER BY _s DESC LIMIT ?"
            )
            # ★ 不吞异常：让 search() 记进 degraded_routes（DESIGN.md #16）
            return self._r._db.query(sql, (ex, ex) + extra_params + (limit,), readonly=True)

        rows = _rows(expr)
        # ★ A4：剪枝后 0 命中 ⇒ 用未剪表达式再打一次（NL 是秒级，LIKE 是秒~十几秒）
        if not rows and raw_expr and raw_expr != expr:
            rows = _rows(raw_expr)
            expr = raw_expr
            if diag is not None:
                diag["retried_unpruned"] = True
            logger.info("fulltext 0 hits after pruning; retried unpruned (%s)", source)
        if not rows:
            return []
        top = max(float(r.get("_s") or 0.0) for r in rows) or 1.0
        if fts_table:
            pairs = [(str(r["guid"]), float(r.get("_s") or 0.0) / top, "fulltext")
                     for r in rows]
            return self._hydrate(source, pairs, query)
        out = []
        for row in rows:
            h = OmnibusHit(
                source=source,
                record_id=str(row.get(spec["id_col"])),
                score=float(row.get("_s") or 0.0) / top,
                matched_by=["fulltext"],
                meta=_row_meta(source, row, self._meta),
            )
            h.snippet = _clip(str(row.get(spec["text_col"]) or ""), query)
            out.append(h)
        return out

    def _phrase(self, source: str, query: str, limit: int) -> list[OmnibusHit]:
        """精确子串路 —— 把 n-gram 倒排当 q-gram 过滤器用（DESIGN #31 档 1）。

        原理：索引侧是 unigram + bigram，**相邻的 bigram 序列就等于原字符串**。
        所以把查询表达式原样包进双引号交给 BOOLEAN MODE 做短语匹配：
          地球化学 -> 查询 "地球 球化 化学" -> 只命中「地球化学」这根连续四字串。
        实测（本库 194k 行）：precision@20 200/200 = 100%，2~10 ms；
          地球化学 6/20 -> 20/20、真空层 0/20 -> 20/20；
          同一件事走 LIKE 要 1.1~1.4 s（`melquiades.harvest` 34 ms vs 1,378 ms）。

        ★ 边界（别写成万能药）：粒度是 **token 序列**，不是字符。
          「melquiades harvest」能命中（index 里是 melquiades + harvest 两 token 相邻），
          但 token 内部的任意子串（`ck_ub`）永远是 0 命中 —— 那只有 LIKE 或字符级
          trigram 索引能解，属于下一版。

        ★ 只在 token 数 ≤ PHRASE_MAX_TOKENS 时挂这条路线：整句问句要求逐 token 相邻
          基本恒为 0 命中，白付一次索引查询。
        """
        spec = self._specs[source]
        expr = build_cjk_query_v2(query, self._stopwords())
        toks = [t for t in expr.split() if t and '"' not in t]
        if not toks or len(toks) > PHRASE_MAX_TOKENS:
            return []
        phrase = '"' + " ".join(toks) + '"'
        fts_table = spec.get("fts_table")
        if fts_table:
            col = spec["tok_col"]
            sql = ("SELECT guid, MATCH(" + col + ") AGAINST(? IN BOOLEAN MODE) AS _s "
                   "FROM " + fts_table + " "
                   "WHERE MATCH(" + col + ") AGAINST(? IN BOOLEAN MODE) "
                   "ORDER BY _s DESC LIMIT ?")
            rows = self._r._db.query(sql, (phrase, phrase, limit), readonly=True)
            if not rows:
                return []
            top = max(float(r.get("_s") or 0.0) for r in rows) or 1.0
            pairs = [(str(r["guid"]), float(r.get("_s") or 0.0) / top, "phrase")
                     for r in rows]
            return self._hydrate(source, pairs, query)
        return []   # 其它源的 tokenized 是另一套口径，短语语义不成立

    # ── 重排（档 3）──────────────────────────────────────────────

    def _rerank_texts(self, hits: list[OmnibusHit]) -> list[str]:
        """按源批量取**正文**喂给重排器；取不到的用占位符（对齐关系不能乱）。"""
        sc = self._cfg_search()
        caps = dict(getattr(sc, "rerank_doc_chars", {}) or {})
        out = ["(空)"] * len(hits)
        by_src: dict[str, list[int]] = {}
        for i, h in enumerate(hits):
            by_src.setdefault(h.source, []).append(i)
        for src, idxs in by_src.items():
            spec = self._specs.get(src)
            if not spec:
                continue
            cap = int(caps.get(src, 400) or 400)
            id_col = spec["id_col"]
            ids = [hits[i].record_id for i in idxs]
            marks = ",".join(["?"] * len(ids))
            try:
                rows = self._r._db.query(
                    "SELECT " + id_col + " AS _id, " + spec["text_col"] + " AS _txt "
                    "FROM " + spec["table"] + " WHERE " + id_col + " IN (" + marks + ")",
                    tuple(ids), readonly=True)
            except Exception as exc:  # noqa: BLE001 —— 取不到正文就退占位符，别拖垮检索
                logger.warning("rerank text fetch failed (%s): %s", src, exc)
                continue
            got = {str(r["_id"]): str(r["_txt"] or "") for r in rows}
            for i in idxs:
                txt = got.get(str(hits[i].record_id), "")
                out[i] = (txt[:cap] if txt else "(空)")
        return out

    def _like(self, source: str, query: str, limit: int) -> list[OmnibusHit]:
        """字面兜底路 —— **保留，但退出排序**（DESIGN.md #14）。

        保留的真实理由（实测得出，别再写错）：
          ① **词内子串**：`v30` / `13228v3` / `muerdagos` —— FULLTEXT 要整词命中；
          ② **脏 tokenized 兜底**：75 行历史数据的变音字符被旧正则丢掉（`Muérdago`→`Mu`+`rdago`），
             这些只有 LIKE 捞得到；
          ③ 重建窗口内的最后一道网。
        保留的理由**不是** collation：实测 LIKE 与 FULLTEXT **都**折叠重音/大小写/上标
          （283/283、5517/5517、251/251、190/190）。

        为什么不给它排序：布尔子串匹配**没有相关性信号**（命中即全等）。
          密度法（tf/len）是 BM25 的退化形式（缺 IDF、缺 k1/b 饱和），与引擎的 BM25 不可比 ——
          拿伪分去融合等于往排序里掺噪声。故 score 固定极低，只作补充集。

        ORDER BY 保留 `id_col DESC`：对 draftpad 是「按日期倒序」（语义正确，当初拍板的就是它），
          对 messages 是随机 —— 但既然不参与排序，随机与否不再影响榜单。
        """
        spec = self._specs[source]
        if not query.strip():
            return []
        extra_sql, extra_params = self._source_extra(source)
        sql = (
            f"SELECT {spec['select']} FROM {spec['table']} "
            f"WHERE {spec['text_col']} LIKE ? ESCAPE '\\\\' AND ({spec['where']}) "
            f"AND ({extra_sql}) ORDER BY {spec['id_col']} DESC LIMIT ?"
        )
        rows = self._r._db.query(
            sql, (f"%{escape_like(query)}%",) + extra_params + (limit,), readonly=True
        )
        out = []
        for row in rows:
            h = OmnibusHit(
                source=source,
                record_id=str(row.get(spec["id_col"])),
                score=LIKE_SUPPLEMENT_SCORE,
                matched_by=["like"],
                meta=_row_meta(source, row, self._meta),
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
        workspace_mode: str = "off",
        current_cwd: str = "",
        rerank: Optional[bool] = None,
    ) -> dict[str, Any]:
        """三路 × 多源，归一化融合。

        ★ 取数口径（2026-10-25 拍板 + 2026-11-07 修订，见 DESIGN.md #17）：

          每路取数    ：`limit` 条（**同分不截断** —— 宁可略多，也不把并列组切一半）
          去重前合计  ：≤ 源数 × 路数 × limit（3×3 时最多 9×limit）
          去重        ：按 (source, record_id) 折叠，留最高分，matched_by 合并
          去重后下限  ：< limit 条不同记录 ⇒ **从更低排名递补**（加深取数重跑一次）
          去重后上限  ：源数 × 路数 × limit（**同分不截**）

        ★ 与 10-25 拍板的关系：那次反对的是「截断回 limit（5 条）」——宽取后窄截会把
          「只有某一路命中、分数偏低」的独特结果挤掉。本次上限是 9×limit（15 条量级），
          仍远大于 limit ⇒ 10-25 的核心不变；新增的只是**下限**。

        返回 {"hits": [...], "stats": {...}, "notes": [...]}
        """
        query = (query or "").strip()
        srcs = [s for s in (sources or DEFAULT_SOURCES) if s in self._specs]
        mds = [m for m in (modes or DEFAULT_MODES) if m in WEIGHTS]
        notes: list[str] = []
        per_route_n = int(per_source_limit or limit)

        if not query:
            return {"hits": [], "stats": {}, "notes": ["查询串为空"]}

        emb = None
        if "vector" in mds and any(self._specs[s]["vec"] for s in srcs):
            emb = self._r.embed(query)
            if emb is None:
                notes.append("向量路不可用（BGE 全部端点失败），本次只走全文与 LIKE")

        diag: dict[str, dict] = {}

        def _fold(per_route: dict[tuple[str, str], list[OmnibusHit]]):
            """把逐路命中折成一张表：同一条记录跨路累加，多路命中给加成。"""
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
            return merged

        def _run_routes(per_n: int, with_like: bool = False):
            """跑一遍所有 (源, 路) 并融合。返回 (merged, per_route, degraded)。

            ★ A5（2026-11-07）：LIKE 路**默认不跑**，只在「全文 + 向量都 0 命中」时
              才降级跑。实测本库 194k 行 / 442 MB body：查 %量子退火% 2.01 s、
              查 %zzqxneverthere% 3.94 s（命中即停 0.03 s）—— 稀词查询每次白付
              2~4 秒，而它的分数固定 0.05（没有相关性信号）。memoria 那边同坑实测 16 s。
            """
            per_route: dict[tuple[str, str], list[OmnibusHit]] = {}
            degraded: list[dict] = []
            for src in srcs:
                plan = []
                if "vector" in mds and emb is not None:
                    plan.append(("vector", lambda s=src: self._vec(s, emb, per_n)))
                if "fulltext" in mds:
                    plan.append(("fulltext",
                                 lambda s=src: self._fulltext(s, query, per_n,
                                                              diag.setdefault(s, {}))))
                if "phrase" in mds and src == "messages":
                    plan.append(("phrase", lambda s=src: self._phrase(s, query, per_n)))
                if "like" in mds and with_like:
                    plan.append(("like", lambda s=src: self._like(s, query, per_n)))
                for mode, fn in plan:
                    # ★ 逐路捕获（#16）：某一路挂了要**说出来**，不能静默返回 0 命中。
                    #   实测风险：桥持一条长连接，WSL relay 抖一次就可能废掉它 ——
                    #   那时三路全空，而 UI 显示的是「没搜到」，不是「桥挂了」。
                    try:
                        per_route[(src, mode)] = fn()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("omnibus route failed (%s/%s): %s", src, mode, exc)
                        degraded.append({"source": src, "route": mode,
                                         "error": type(exc).__name__ + ": " + str(exc)[:160]})
                        per_route[(src, mode)] = []

            return _fold(per_route), per_route, degraded

        # 明确的 like-only 请求（modes=("like",)）就直接跑，不走降级逻辑
        like_first = ("like" in mds) and not any(
            m in mds for m in ("vector", "fulltext", "phrase"))
        merged, per_route, degraded = _run_routes(per_route_n, with_like=like_first)

        # ── A5：**全文全空** ⇒ 才降级到 LIKE 字面兜底 ──
        #   ★ 判据修订（审计文档写的是「全文 + 向量都空」）：向量路没有阈值，
        #     任何查询都能返回 top-k —— 按原判据 LIKE 永远不可达，等于把这条
        #     唯一能捞「标识符片段 / 路径子串」的路**静默删掉**。
        #     真正的信号是「字面上一个都没匹配上」，所以看全文路。
        if "like" in mds and not like_first and not any(
                per_route.get((src, "fulltext")) for src in srcs):
            for src in srcs:
                try:
                    per_route[(src, "like")] = self._like(src, query, per_route_n)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("omnibus route failed (%s/like): %s", src, exc)
                    degraded.append({"source": src, "route": "like",
                                     "error": type(exc).__name__ + ": " + str(exc)[:160]})
            # ★ 只把 like 的命并进来，**不能重折整张表**：per_route 里存的是同一批
            #   OmnibusHit 对象，重折会对已经乘过权重的向量/全文命中再乘一次。
            for key, hit in _fold({k: v for k, v in per_route.items()
                                   if k[1] == "like"}).items():
                cur = merged.get(key)
                if cur is None:
                    merged[key] = hit
                else:
                    cur.score += hit.score
                    cur.matched_by += [m for m in hit.matched_by
                                       if m not in cur.matched_by]
            if merged:
                notes.append("全文与向量均 0 命中，已降级到 LIKE 字面兜底"
                             "（无相关性信号，权重 %.2f）" % WEIGHTS["like"])

        # ── 下限保证：去重后不足 limit 条 ⇒ 加深取数重跑一次（#17「递补」）──
        #   ★ 守卫 `merged` 非空：0 命中时加深**必然还是 0**（LIMIT 5 都拿不到，
        #     LIMIT 20 更拿不到），白白把 LIKE 的全表扫跑两遍（实测单次可达 2s）。
        #     真正需要递补的是「各路取到的行高度重合，去重后塌缩」那种情形。
        if 0 < len(merged) < limit:
            deep = min(max(per_route_n * DEEPEN_FACTOR, limit * DEEPEN_FACTOR), MAX_PER_ROUTE)
            if deep > per_route_n:
                before = len(merged)
                merged, per_route, degraded = _run_routes(deep, with_like=like_first)
                per_route_n = deep
                notes.append("去重后仅 %d 条 < limit=%d，已加深取数至每路 %d 条（现 %d 条）"
                             % (before, limit, deep, len(merged)))

        # ── 工作区作用（#15）：默认 boost，可选 filter，绝不猜 ──
        #   为什么默认是加权而不是硬过滤：跨工作区找回是真实需求
        #   （本次排查就是从 melquiades 找冰质星球的东西），硬过滤会让这个能力消失。
        if workspace_mode in ("boost", "filter") and current_cwd:
            cur = os.path.normpath(current_cwd)

            def _in_workspace(h) -> bool:
                wp = (h.meta or {}).get("workspace_path") or ""
                return bool(wp) and os.path.normpath(wp) == cur

            if workspace_mode == "filter":
                kept = [h for h in merged.values()
                        if _in_workspace(h) or not (h.meta or {}).get("workspace_path")]
                notes.append("工作区硬过滤：%d → %d 条（无工作区信息的结果保留）"
                             % (len(merged), len(kept)))
                merged = {(h.source, h.record_id): h for h in kept}
            else:
                boosted = 0
                for h in merged.values():
                    if _in_workspace(h):
                        h.score *= WORKSPACE_BOOST
                        boosted += 1
                if boosted:
                    notes.append("同工作区加权 ×%.2f：%d 条" % (WORKSPACE_BOOST, boosted))

        ranked = sorted(merged.values(), key=lambda x: -x.score)

        # ── 档 3：跨编码器精排（DESIGN #31）──────────────────────────
        # ★ 它只治「排得准」，一格召回都补不了（池外的东西它看不见）—— 所以必须
        #   在候选集已经定了之后才排，绝不能拿它替代短语路/稀有词那类召回手段。
        # ★ 只对**池内**重排，池外保持融合序追加：重排失败的退路就是融合序本身。
        sc = self._cfg_search()
        want_rr = bool(getattr(sc, "rerank_enabled", False)) if rerank is None else bool(rerank)
        rr_info: dict[str, Any] = {"applied": False}
        if (self._rr is not None and want_rr and len(ranked) > 1
                and any(m in mds for m in ("vector", "fulltext", "phrase"))):
            pool = min(int(getattr(sc, "rerank_pool", 30) or 30), len(ranked))
            head = ranked[:pool]
            docs = self._rerank_texts(head)
            outcome = self._rr.score(query, docs)
            if outcome.ok:
                ordered = [head[i] for i in outcome.order if 0 <= i < len(head)]
                for pos, i in enumerate(outcome.order):
                    if 0 <= i < len(head) and pos < len(outcome.scores):
                        head[i].rerank_score = outcome.scores[pos]
                before_ids = {h.record_id for h in head[:limit]}
                after_ids = {h.record_id for h in ordered[:limit]}
                ranked = ordered + ranked[pool:]
                rr_info = {"applied": True, "pool": pool, "moved": len(after_ids - before_ids),
                           "model": getattr(sc, "rerank_model", ""),
                           "latency_ms": outcome.latency_ms, "tokens": outcome.tokens}
                notes.append("跨编码器重排：池 %d 条，top-%d 换入 %d 条（%d ms）"
                             % (pool, limit, rr_info["moved"], outcome.latency_ms))
            else:
                # 失败必须说出来（铁律：显式降级）—— 不许静默按融合序交差
                rr_info = {"applied": False, "error": (outcome.error or "")[:200]}
                notes.append("⚠ 重排失败，已退回融合序：%s" % (outcome.error or "")[:120])

        # ── 上限：源数 × 路数 × limit，**同分不截** ──
        hard_cap = max(limit * len(srcs) * len(mds), limit)
        if len(ranked) > hard_cap:
            cut = ranked[hard_cap - 1].score
            kept = [h for h in ranked if h.score >= cut]
            notes.append("去重后 %d 条，超出上限 %d，按分数截断（同分保留）→ %d 条"
                         % (len(ranked), hard_cap, len(kept)))
            ranked = kept

        stats = {
            "sources": {s: len([h for h in ranked if h.source == s]) for s in srcs},
            "routes": {m: sum(len(v) for (s, mm), v in per_route.items() if mm == m) for m in mds},
            "vector_available": emb is not None,
            "per_route_limit": per_route_n,
            "hard_cap": hard_cap,
            "returned_after_dedup": len(ranked),
            "degraded_routes": degraded,
            # ★ A3：把「查询侧到底用了哪些词、丢了哪些词」变成可审计对象。
            #   这次误伤的全部损失原本都是静默的 —— 剪枝必须在第一眼被看到。
            "query_terms": diag,
            "rerank": rr_info,
        }
        if degraded:
            notes.append("⚠ 有 %d 条检索路失败（结果可能不全）：%s"
                         % (len(degraded),
                            "; ".join(d["source"] + "/" + d["route"] for d in degraded)))
        if not self._specs["messages"]["vec"]:
            notes.append(
                "会话正文的向量列存在（已嵌 16.5%），但**没有 VECTOR 索引** —— "
                "无索引时 VEC_DISTANCE_COSINE 退化为全表暴力扫 20.6 万×1024 维，"
                "故该源当前只走全文 + LIKE；语义召回由草稿纸与压缩摘要承担"
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
