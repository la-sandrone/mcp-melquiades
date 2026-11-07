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
DEFAULT_MODES = ("vector", "fulltext", "like")

# 融合权重：向量偏"像"，FULLTEXT 偏"准"，LIKE 是字面兜底。
# 同一条被多路命中时额外加成（每多一路 +0.08），避免只靠单路的条目压倒多路共识。
WEIGHTS = {"vector": 0.45, "fulltext": 0.40, "like": 0.15}
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

    def _fulltext(self, source: str, query: str, limit: int) -> list[OmnibusHit]:
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
        if source == "messages":
            expr = build_cjk_query_v2(query, self._stopwords())
            mode = "NATURAL LANGUAGE MODE"
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
        if fts_table:
            col = spec["tok_col"]
            sql = ("SELECT guid, MATCH(" + col + ") AGAINST(? IN " + mode + ") AS _s "
                   "FROM " + fts_table + " "
                   "WHERE MATCH(" + col + ") AGAINST(? IN " + mode + ") "
                   "ORDER BY _s DESC LIMIT ?")
            rows = self._r._db.query(sql, (expr, expr, limit), readonly=True)
            if not rows:
                return []
            top = max(float(r.get("_s") or 0.0) for r in rows) or 1.0
            pairs = [(str(r["guid"]), float(r.get("_s") or 0.0) / top, "fulltext")
                     for r in rows]
            return self._hydrate(source, pairs, query)

        sql = (
            f"SELECT {spec['select']}, MATCH({spec['tok_col']}) AGAINST(? IN {mode}) AS _s "
            f"FROM {spec['table']} "
            f"WHERE MATCH({spec['tok_col']}) AGAINST(? IN {mode}) AND ({spec['where']}) "
            f"AND ({extra_sql}) ORDER BY _s DESC LIMIT ?"
        )
        # ★ 不吞异常：让 search() 记进 degraded_routes（DESIGN.md #16）
        rows = self._r._db.query(sql, (expr, expr) + extra_params + (limit,), readonly=True)
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
                meta=_row_meta(source, row, self._meta),
            )
            h.snippet = _clip(str(row.get(spec["text_col"]) or ""), query)
            out.append(h)
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

        def _run_routes(per_n: int):
            """跑一遍所有 (源, 路) 并融合。返回 (merged, per_route, degraded)。"""
            per_route: dict[tuple[str, str], list[OmnibusHit]] = {}
            degraded: list[dict] = []
            for src in srcs:
                plan = []
                if "vector" in mds and emb is not None:
                    plan.append(("vector", lambda s=src: self._vec(s, emb, per_n)))
                if "fulltext" in mds:
                    plan.append(("fulltext", lambda s=src: self._fulltext(s, query, per_n)))
                if "like" in mds:
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
            return merged, per_route, degraded

        merged, per_route, degraded = _run_routes(per_route_n)

        # ── 下限保证：去重后不足 limit 条 ⇒ 加深取数重跑一次（#17「递补」）──
        #   ★ 守卫 `merged` 非空：0 命中时加深**必然还是 0**（LIMIT 5 都拿不到，
        #     LIMIT 20 更拿不到），白白把 LIKE 的全表扫跑两遍（实测单次可达 2s）。
        #     真正需要递补的是「各路取到的行高度重合，去重后塌缩」那种情形。
        if 0 < len(merged) < limit:
            deep = min(max(per_route_n * DEEPEN_FACTOR, limit * DEEPEN_FACTOR), MAX_PER_ROUTE)
            if deep > per_route_n:
                before = len(merged)
                merged, per_route, degraded = _run_routes(deep)
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
