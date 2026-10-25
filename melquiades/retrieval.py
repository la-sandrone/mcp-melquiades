"""
文件名: melquiades/retrieval.py
摘要: 检索栈 —— **多路 + 显式兜底链 + 线索路**（设计稿 §5.5；2026-09-22 实测后扩为四路）。

五条路（mode）:
  ① auto      融合：向量 + FULLTEXT + **线索路**加权并置（默认）
  ② vector    BGE 余弦相似（只搜已嵌入行：embedded = 1 —— VECTOR 列比较不可靠）
  ③ fulltext  MariaDB FULLTEXT BOOLEAN MODE（**OR 语义**，见 tokenizer 漂移记录）
  ④ clue      **逐线索检索 + IDF 过滤 + 集成**（2026-09-22 新增，参数见 config.search.clue_*）
  ⑤ keyword   LIKE 串联（多关键词 OR）—— 精确字面、写法不定
  ⑥ condition 原样 SQL 条件透传 —— 数值布尔矩阵审计

兜底链（auto 模式）:
    向量 + 全文 + 线索 融合 → 空？ → LIKE 串联 → 仍空？ → 显式报告未命中（不静默空手而归）

实测依据:
  · 真实调用中 MATCH() 0 次、LIKE 169 次（33%），其中纯中文主题词 63%、数值探针 5%
  · 单条 SQL 最多 14 个 LIKE ⇒ 多关键词串联是真实高频用法
  · 零向量行对真实查询向量的距离为 0.0（会排最前）⇒ **必须过滤未嵌入行**
  · 过滤后全表向量扫 4.1ms（可接受）
  · ★ 2026-09-22 修正：上面「零向量会排最前」是对的，但据此推出的
    `embedding != VEC_FromText(...)` 过滤**实际无效**（VECTOR 类型上该比较
    不可靠）⇒ 29/103 行零向量照样混进语义检索、因 dist=0 排最前、拿假最高
    分 1/(1+0)=1.0。现改用独立标记列 `embedded`（schema.ensure_embedded_column）。

依赖: dbcore.py, config.py, tokenizer.py, httpx
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from melquiades.config import Config
from melquiades.dbcore import MariaDB
from melquiades.rerank import Reranker
from melquiades.schema import ZERO_VECTOR
from melquiades.sqlguard import (
    COLS_COMPACTION,
    COLS_DRAFTPAD,
    DISPLAY_TEXT_CHARS,
    guard_condition,
    guard_select_cols,
    guard_table,
)
from melquiades.tokenizer import (
    build_cjk_boolean,
    escape_like,
    has_cjk,
    split_clues,
    split_keywords,
    tokenize_query,
)

logger = logging.getLogger(__name__)

# 检索路径标识
MODE_AUTO = "auto"
MODE_VECTOR = "vector"
MODE_FULLTEXT = "fulltext"
MODE_CLUE = "clue"
MODE_KEYWORD = "keyword"
MODE_CONDITION = "condition"
ALL_MODES = (MODE_AUTO, MODE_VECTOR, MODE_FULLTEXT, MODE_CLUE, MODE_KEYWORD,
             MODE_CONDITION)

# `_merge` 的兜底 RRF 常数（外部调用方不传 k 时用）。cfg.search.rrf_k 才是生产口径。
_DEFAULT_RRF_K = 10

# 各表默认 SELECT 列清单 —— 直接引用 sqlguard 的唯一真相源（整串白名单匹配）
_DEFAULT_COLS: dict[str, str] = {
    "draftpad": COLS_DRAFTPAD,
    "dsh_compaction": COLS_COMPACTION,
}

# 各表默认软删列（"auto" ⇒ 按表推断；None ⇒ 该表无软删）
_DEFAULT_DELETED_COL: dict[str, Optional[str]] = {
    "draftpad": "deleted_at",
    "dsh_compaction": None,
}

# 各表的**正文列**——重排必须取正文，不能复用展示口径（见 rerank.fetch_texts）
_TEXT_COL: dict[str, str] = {
    "draftpad": "content",
    "dsh_compaction": "summary",
}

# 正文为空的候选喂给重排器时的占位符（空串可能被对端拒绝，且对齐关系不能乱）
_EMPTY_DOC_PLACEHOLDER = "(空)"


@dataclass
class Hit:
    """一条检索命中。"""

    id: int
    score: float
    match_type: str
    table: str = "draftpad"
    session_id: str = ""
    note_key: str = ""
    content: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: Any = None
    # 本路**原始**相似度 / 匹配分（不被 _merge 的排名分覆盖）——
    # score 是融合排序用的加权分，raw_score 才是「这条到底像不像」的依据
    raw_score: float = 0.0
    # 跨编码器重排分（未重排时恒为 0）。**单独一列而不覆盖 score**：
    # score 是多路融合的证据，rr_score 是 (query×doc) 交互打分，两者语义不同，
    # 混成一列会让「这条为什么排前面」无法回答。
    rr_score: float = 0.0
    # 本行**是否参与了**重排。池外的行不参与 ⇒ rr_score 无意义，
    # 渲染时**不可把它当 0 分读**（故用独立布尔，而不是靠 rr_score == 0 判断）。
    reranked: bool = False
    # dsh_compaction 专有（其它表恒为 0）—— 判断档案量级用
    turn_no: int = 0
    shadowed_count: int = 0
    shadowed_tokens: int = 0


@dataclass
class FindMeta:
    """检索口径元信息 —— **降级必须可观测**（铁律四）。"""

    mode_requested: str = MODE_AUTO
    mode_used: str = ""
    chain: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # 查询信息量体检（2026-09-22）。**只报数、不自动改变行为**：
    # 搜索的本质是用信息消除不确定性；信息不够时正确做法是"再问问用户"，
    # 而不是在关键词上玩数字和公式（吴军《数学之美》）。故这里只产出一个可显示的
    # 体检结果，由调用方/LLM 决定要不要补充线索——**不自动拒答、不自动换路**。
    uncertainty: dict = field(default_factory=dict)
    # 跨编码器重排状态（requested / applied / pool / ok / error / latency_ms / moved。
    # **失败必须留痕**：重排挂了要能在工具输出里看见，不能装作排过）
    rerank: dict = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        """是否发生了降级（请求的 mode 与实际不完全一致）。"""
        return len(self.chain) > 1


@dataclass
class FindResult:
    hits: list[Hit] = field(default_factory=list)
    meta: FindMeta = field(default_factory=FindMeta)

    @property
    def empty(self) -> bool:
        return not self.hits

    def explain(self) -> str:
        """人类可读的口径说明（供工具返回给 LLM）。"""
        m = self.meta
        parts = [f"mode={m.mode_requested}→{m.mode_used}"]
        if m.counts:
            parts.append("counts=" + ",".join(f"{k}:{v}" for k, v in m.counts.items()))
        if m.degraded:
            parts.append("chain=" + " → ".join(m.chain))
        if m.notes:
            parts.append("notes=" + "; ".join(m.notes))
        if m.rerank.get("applied"):
            rr = m.rerank
            # 标签必须说真话：moved 是"top-limit 里有几条是新换进来的"，
            # 不是"有几条挪了位置"。写成"动N条"会让人以为位置微调。
            parts.append(f"rerank=池{rr.get('pool')}/换入{rr.get('moved')}条/"
                         f"{rr.get('latency_ms')}ms")
        elif m.rerank.get("requested") and not m.rerank.get("applied"):
            parts.append(f"rerank=未生效({m.rerank.get('why', '?')})")
        return " | ".join(parts)


class FulltextPathError(RuntimeError):
    """全文路失败 —— **不要吞掉**：失败与「无命中」是两件事，必须上浮给调用方。"""


class Retriever:
    """多路检索器（draftpad / dsh_compaction 通用）。"""

    def __init__(self, cfg: Config, db: MariaDB) -> None:
        self._cfg = cfg
        self._db = db
        self._client: Optional[httpx.Client] = None
        self._endpoint_in_use: str = ""
        self._reranker = Reranker(cfg.search)

    # ── HTTP（嵌入）──

    def _client_for(self, base: str) -> httpx.Client:
        return httpx.Client(
            base_url=base.rstrip("/"),
            timeout=httpx.Timeout(
                self._cfg.search.bge_timeout_sec,
                connect=self._cfg.search.bge_connect_timeout_sec,
            ),
        )

    def embed(self, text: str) -> Optional[list[float]]:
        """
        取文本向量 —— **多端点按序尝试**（9005 → 9006），全败返回 None（调用方降级）。
        """
        if not text.strip():
            return None
        payload = {"input": text, "model": self._cfg.search.bge_model}
        bases = list(self._cfg.search.bge_endpoints)
        # 成功过的端点优先 —— 避免每轮都在不可用端点上白等超时
        if self._endpoint_in_use and self._endpoint_in_use in bases:
            bases.remove(self._endpoint_in_use)
            bases.insert(0, self._endpoint_in_use)
        tried: list[str] = []
        for base in bases:
            try:
                with self._client_for(base) as cli:
                    resp = cli.post("/v1/embeddings", json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    emb = data["data"][0]["embedding"]
                    if isinstance(emb, list) and emb:
                        self._endpoint_in_use = base
                        return [float(x) for x in emb]
                    tried.append(f"{base}:empty")
            except Exception as exc:
                tried.append(f"{base}:{type(exc).__name__}")
        logger.warning("embed failed on all endpoints: %s", ", ".join(tried))
        return None

    # ── 主入口 ──

    def find(
        self,
        query: str,
        *,
        mode: str = MODE_AUTO,
        table: str = "draftpad",
        bucket: Optional[str] = None,
        tags: Optional[list[str]] = None,
        limit: int = 5,
        condition: str = "",
        condition_params: Optional[list] = None,
        select_cols: str = "",
        deleted_col: Optional[str] = "auto",
        rerank: Optional[bool] = None,
        extra_terms: Optional[list[str]] = None,
        extra_note: str = "",
    ) -> FindResult:
        """
        多路检索。

        参数:
          query     — 查询串（vector/fulltext/keyword 用）
          mode      — auto | vector | fulltext | clue | keyword | condition
          table     — draftpad | dsh_compaction
          bucket    — 限定 session_id（None = 跨桶，默认行为）
          tags      — 标签过滤（LIKE 匹配 tags 列）
          limit     — 返回条数
          condition — mode=condition 时的原样 SQL 条件（**由调用方负责安全**）
          select_cols — SELECT 列清单（不同表列不同）
          rerank    — 跨编码器精排。None = 跟随配置（默认开）；False = 本次关掉。
                      **不作用于** mode=keyword / mode=condition / auto 的 LIKE 兜底分支。
          extra_terms — **词表查询扩展**：同义词环里的其它写法（如查 `裁决` 时补上
                      `裁定`）。★ 只追加给**字面两路**（全文/线索），**不动向量路** ——
                      同义词要补的洞是"字面对不上"，往向量查询里塞同义词会稀释原查询。
                      由调用方（工具层）决定要不要扩展；本函数只管机制。
        """
        # ── 输入卫生（早失败；标识符无法参数化 ⇒ 白名单）──
        table = guard_table(table)
        # 不传列清单/软删列时**按表自动推断**
        # （避免调用方拿 draftpad 的列或 deleted_at 去查 dsh_compaction）
        select_cols = select_cols or _DEFAULT_COLS.get(table, "")
        select_cols = guard_select_cols(select_cols)
        if deleted_col == "auto":
            deleted_col = _DEFAULT_DELETED_COL.get(table)
        if condition:
            condition = guard_condition(condition)

        meta = FindMeta(mode_requested=mode)
        # ── 重排开关 ──
        # 语义定死：**配置是总闸，参数只能关不能开**。
        # `rerank_enabled=false` 是事故急停开关，若允许调用方传 True 绕过，
        # 它就只是"建议"而不是开关了（出事故时最需要它可靠）。
        # 注意 fetch_n：要重排就必须**多取几条**当候选池（重排只能重排看得见的东西），
        # 否则"池"就等于 limit，重排退化成"把 5 条重排一遍"。
        cap = int(self._cfg.search.rerank_doc_chars.get(table, DISPLAY_TEXT_CHARS))
        table_on = bool(self._cfg.search.rerank_tables.get(table, False))
        if not self._reranker.enabled:
            use_rerank = False
            if rerank:
                meta.notes.append(
                    "配置 rerank_enabled=false（急停）：传 rerank=True 不生效")
        elif not table_on:
            # 按表停用（档案表即此情形）。**必须留痕**：否则调用方看到"没重排"
            # 会以为是自己传错了参数，而不是配置如此。
            use_rerank = False
            if rerank is not False:
                meta.notes.append(
                    f"配置 rerank_tables[{table}]=false ⇒ 该表不重排"
                    f"（实测该表无重排信号，见 config.yaml 注释）")
        else:
            use_rerank = True if rerank is None else bool(rerank)
        fetch_n = (max(limit, int(self._cfg.search.rerank_pool))
                   if use_rerank else limit)
        meta.rerank = {"requested": bool(use_rerank), "applied": False,
                       "table": table, "doc_chars": cap,
                       "pool": 0, "moved": 0, "latency_ms": 0, "error": ""}
        where_extra, params_extra, notes = self._build_filters(bucket, tags)
        # 软删过滤：draftpad 有 deleted_at；dsh_compaction 没有（传 deleted_col=None）
        if deleted_col:
            where_extra = f" AND {deleted_col} IS NULL" + where_extra
        meta.notes.extend(notes)

        # ⑤ condition：原样透传（数值探针/布尔矩阵审计）
        # ★ 2026-09-22 修正：此前 condition **只在 mode=condition 下生效**，
        #   其它模式下被静默丢弃 —— 传了 condition 却拿到「无条件检索结果」，
        #   看起来像「过滤后恰好为空」。现在：
        #     · query 为空 + condition 非空 ⇒ **自动切到 condition 模式**（列举/探针）
        #     · query 非空 + condition 非空 ⇒ 作为附加 WHERE **并入检索各路**
        cond = (condition or "").strip()
        if cond and not query.strip():
            meta.notes.append("query 为空且给了 condition ⇒ 自动改用 condition 模式")
            mode = MODE_CONDITION
        elif cond:
            where_extra = f"{where_extra} AND ({cond})"
            params_extra = (*params_extra, *(condition_params or []))
            meta.notes.append("condition 已并入检索附加过滤")

        if mode == MODE_CONDITION:
            # 提前 return 也要把"为何没重排"留痕，否则 explain() 会印
            # `rerank=未生效(?)` —— 留痕不全等于没留痕
            meta.rerank.update({
                "requested": False,
                "why": "mode=condition（原样 SQL 探针，排序由调用方的 SQL 决定）不走重排",
            })
            if not cond:
                meta.notes.append("condition 为空")
                meta.mode_used = MODE_CONDITION
                return FindResult([], meta)
            sql = (f"SELECT {select_cols} FROM {table} "
                   f"WHERE 1=1 {where_extra} AND ({cond}) "
                   f"ORDER BY id LIMIT ?")
            # condition 中的 `?` 由 condition_params 填充 ⇒ 片段也参数化
            # 参数顺序：where_extra 的占位符在前，condition 的在后
            hits = self._run_plain(
                sql, (*params_extra, *(condition_params or []), limit), table)
            meta.mode_used = MODE_CONDITION
            meta.counts = {MODE_CONDITION: len(hits)}
            meta.notes.append(str(meta.rerank.get("why") or ""))
            return FindResult(hits, meta)

        scored: dict[int, Hit] = {}

        # ── 词表查询扩展：只并进**字面两路** ──
        terms = [t for t in (extra_terms or []) if t and t.strip()]
        lex_q = query if not terms else (query + " " + " ".join(terms))
        if terms:
            # 说明由**工具层**给（它才知道扩展词各自来自等价路还是层级路）——
            # 本函数不知道来源，硬编一句"可能含下位概念"在等价扩展时就是噪声。
            tail = extra_note or f"并入 {len(terms)} 个词（{'、'.join(terms[:6])}）"
            meta.notes.append(
                f"查询扩展：{tail}——只作用于全文/线索路，向量路未动")

        # ② vector
        if mode in (MODE_AUTO, MODE_VECTOR):
            vh = self._vector_search(query, table, select_cols, where_extra,
                                     params_extra, fetch_n)
            meta.chain.append(f"{MODE_VECTOR}:{len(vh)}")
            meta.counts[MODE_VECTOR] = len(vh)
            if not vh:
                meta.notes.append("向量路无命中（未嵌入 / BGE 不可用 / 确实不相关）")
            self._merge(scored, vh, self._cfg.search.vector_weight,
                       self._cfg.search.rrf_k)

        # ③ fulltext
        if mode in (MODE_AUTO, MODE_FULLTEXT):
            try:
                fh = self._fulltext_search(lex_q, table, select_cols, where_extra,
                                           params_extra, fetch_n)
            except FulltextPathError as exc:
                # **失败 ≠ 无命中**：显式区分并上浮（不静默降级）
                fh = []
                meta.notes.append(str(exc))
            meta.chain.append(f"{MODE_FULLTEXT}:{len(fh)}")
            meta.counts[MODE_FULLTEXT] = len(fh)
            if not fh and not any("全文路失败" in n for n in meta.notes):
                meta.notes.append(
                    "全文路无命中（innodb_ft_min_token_size=3：2 字中文词不进"
                    "索引，纯 2 字词请改用 keyword 路）")
            self._merge(scored, fh, self._cfg.search.fts_weight,
                       self._cfg.search.rrf_k)

        # ③b 线索路：逐线索检索 + IDF 过滤 + 集成（与全文路**并进**，不替代）
        #     实测：OR 单路 hit@5 145 → 并进线索路 151（REPORT.md §八）
        #     只在 auto / 显式 clue 模式下跑：`mode=fulltext` 应当就是"只走全文路"，
        #     往里塞第二条词法路会让模式名与实际行为不符。
        if mode in (MODE_AUTO, MODE_CLUE):
            ch, clue_notes, clue_stats = self._clue_search(
                lex_q, table, select_cols, where_extra, params_extra)
            meta.chain.append(f"{MODE_CLUE}:{len(ch)}")
            meta.counts[MODE_CLUE] = len(ch)
            meta.notes.extend(clue_notes)
            meta.uncertainty["clue_stats"] = clue_stats
            self._merge(scored, ch,
                        self._cfg.search.fts_weight
                        * self._cfg.search.clue_weight_ratio,
                        self._cfg.search.rrf_k)

        # ④ keyword（显式请求 或 auto 的兜底）
        fallback_like = False
        do_keyword = (mode == MODE_KEYWORD) or (
            mode == MODE_AUTO and not scored and self._cfg.search.fallback_to_like
        )
        if do_keyword:
            kh = self._keyword_search(query, table, select_cols, where_extra,
                                      params_extra, fetch_n)
            tag = MODE_KEYWORD if mode == MODE_KEYWORD else f"{MODE_KEYWORD}(兜底)"
            meta.chain.append(f"{tag}:{len(kh)}")
            meta.counts[MODE_KEYWORD] = len(kh)
            if mode == MODE_AUTO:
                fallback_like = True
                meta.notes.append("融合路无命中，已降级 LIKE 串联（结果按字面匹配，非语义）")
            self._merge(scored, kh, self._cfg.search.keyword_weight,
                       self._cfg.search.rrf_k)

        if not scored:
            meta.notes.append("全部路径均未命中（各路皆空）")

        ranked = sorted(scored.values(), key=lambda h: h.score, reverse=True)
        # 融合序前 limit 条的快照 —— 供体检用（重排会打乱 ranked，见下方注释）
        fusion_top = ranked[:limit]

        # ── 跨编码器精排：在**融合序候选池**上重排，再截到 limit ──
        # 精排只治"排得准"，**一格都补不了召回**（池外的它看不见）——
        # 故池大小必须 >= limit，否则重排退化成"把 limit 条重排一遍"。
        if use_rerank:
            why = ""
            if mode == MODE_KEYWORD:
                why = "mode=keyword（LIKE 串联）不走重排"
            elif fallback_like:
                why = "auto 的 LIKE 兜底分支不走重排（兜底＝退到字面查找）"
            elif not ranked:
                why = "无候选"
            if why:
                meta.rerank["requested"] = False
                meta.rerank["why"] = why
                meta.notes.append(f"重排未生效：{why}")
            else:
                ranked = self._rerank_hits(query, table, ranked, fetch_n, limit, meta)

        # ★ 体检必须在**重排之前**做，喂**融合序**而不是返回序。
        #   理由：sep5 的阈值 0.316 是按「融合分的分布」标定的（232 条实测 p25），
        #   而重排后顺序已变、融合分不再单调 ⇒ (s0-s4)/s0 会变成**负数**
        #   （活体验证抓到 -0.714）⇒ 每个重排过的查询都误报「区分度低」。
        #   这又是一次"阈值跨口径移植"——第一次是跨模式，这次是跨排序阶段。
        health_hits = ranked[:limit] if not use_rerank else fusion_top
        meta.uncertainty.update(self._query_health(meta, health_hits))
        hits = ranked[:limit]
        meta.mode_used = mode
        return FindResult(hits, meta)

    def _rerank_hits(
        self, query: str, table: str, ranked: list[Hit],
        pool_n: int, limit: int, meta: FindMeta,
    ) -> list[Hit]:
        """在融合序前 pool_n 条上重排；池外保持原序追加在后（**不丢候选**）。

        失败时：保留融合序 + 把原因写进 meta（`rerank.ok=False`）——
        **显式降级，绝不静默**。

        ★ 降级 ≠ 关掉重排（验收时实测发现，原注释写错了）：
          重排**开启**时 fetch_n = max(limit, pool)（本例 30），关掉时 fetch_n = limit。
          故降级得到的是「**同一候选池(30)** 上的融合序」，
          而 rerank=False 得到的是「5 条池上的融合序」——**两者候选集不同**。
          降级结果的正确等价物是 `find(..., limit=pool, rerank=False)` 的前 limit 条。
          这不是缺陷：降级的候选池更大（召回不差），且严格是融合序（未精修）。
        """
        pool = ranked[:pool_n]
        rest = ranked[pool_n:]
        # ★ 要不要额外取正文，取决于"要的字数是否超过 Hit.content 的展示宽度"。
        #   实测（193 条行为标签，生产路径，池=30）：
        #     doc_chars=400  → hit@5 128 / MRR 0.5303 / p50  655ms   ← 全轴最优
        #     doc_chars=800  → hit@5 126~127 / MRR ~0.5091 / p50 2260ms
        #     doc_chars=1200 → hit@5 123 / MRR 0.4942 / p50 3472ms
        #   随长度**单调变差**（文献侧对应 MultiConIR：重排器随文档长度衰减，
        #   注意力弥散），且成本也随长度涨 ⇒ 无取舍，取 400。
        #   而 400 恰是展示宽度 ⇒ **不必再查一次库**（省一次 DB 往返，
        #   在 SYN 丢包那件事上也不是坏事）。只有把 rerank_doc_chars 调**大**时
        #   才需要真的去取正文。
        cap = int(meta.rerank.get("doc_chars") or DISPLAY_TEXT_CHARS)
        if cap <= DISPLAY_TEXT_CHARS:
            texts: dict[int, str] = {}
        else:
            texts = self._reranker.fetch_texts(
                self._db, table, [h.id for h in pool],
                _TEXT_COL.get(table, "content"), cap)
        docs: list[str] = []
        for h in pool:
            txt = (texts.get(h.id) or h.content or "")[:cap]
            if not txt.strip():
                txt = _EMPTY_DOC_PLACEHOLDER
            docs.append(txt)

        out = self._reranker.score(query, docs)
        meta.rerank.update({
            "applied": out.ok,
            "pool": len(pool),
            "ok": out.ok,
            "error": out.error,
            "latency_ms": out.latency_ms,
            "tokens": out.tokens,
        })
        if not out.ok:
            meta.notes.append(
                f"重排失败已降级为融合序（查询条数不变，仅排序未精修）：{out.error[:120]}")
            meta.rerank["moved"] = 0
            return ranked

        for i, h in enumerate(pool):
            h.rr_score = out.scores[i] if i < len(out.scores) else 0.0
            h.reranked = True
        before = {h.id for h in ranked[:limit]}
        reordered = [pool[i] for i in out.order] + rest
        # moved = 换入条数（top-limit 里原先不在的）。见 explain() 的标签说明。
        meta.rerank["moved"] = sum(1 for h in reordered[:limit] if h.id not in before)
        return reordered

    # ── 各路实现 ──

    def _vector_search(
        self, query: str, table: str, select_cols: str,
        where_extra: str, params_extra: tuple, limit: int,
    ) -> list[Hit]:
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

        emb = self.embed(query)
        if emb is None:
            return []
        if len(emb) != self._cfg.search.embedding_dim:
            logger.warning("embedding dim mismatch: %d != %d",
                           len(emb), self._cfg.search.embedding_dim)
            return []
        vec_lit = json.dumps(emb)

        # 关键：向量放进派生表解析一次（避免逐行重解析）。
        # ★ 未嵌入行的排除**必须用 embedded 标记列**，不能用
        #   `embedding != VEC_FromText(...)` —— 2026-09-22 实测该比较在
        #   VECTOR 类型上不可靠：dsh_compaction 的 29 行零向量既没被它排出，
        #   也没被 backfill 的 `=` 找到，结果零向量因 dist=0 排在**最前**并
        #   拿到假最高分 1/(1+0)=1.0 —— 语义检索返回的其实是最不相关的一批。
        sql = (
            f"SELECT {select_cols}, "
            f"  VEC_DISTANCE_COSINE(embedding, q.qv) AS _dist "
            f"FROM {table}, (SELECT VEC_FromText(?) AS qv) q "
            f"WHERE embedded = 1 {where_extra} "
            f"ORDER BY _dist ASC LIMIT ?"
        )
        rows = self._db.query(sql, (vec_lit, *params_extra, limit),
                              readonly=True)
        out: list[Hit] = []
        max_d = self._cfg.search.vector_max_distance or 0.0
        for r in rows:
            dist = float(r.get("_dist") or 0.0)
            if max_d > 0 and dist > max_d:
                continue
            h = self._row_to_hit(r, table)
            h.score = h.raw_score = 1.0 / (1.0 + dist)
            h.match_type = MODE_VECTOR
            out.append(h)
        return out

    def _fulltext_search(
        self, query: str, table: str, select_cols: str,
        where_extra: str, params_extra: tuple, limit: int,
    ) -> list[Hit]:
        """
        FULLTEXT BOOLEAN MODE。

        ★ 2026-10-25 口径切换（推翻 2026-09-22 的结论）：
          当时因为 `innodb_ft_min_token_size = 3` 把 2 字 token 全丢，
          `MATCH(content_tokenized) '+口径*'` = 0 命中，只好弃用分词列、
          改走 `content` 原文列（前缀匹配）。**那等于放弃了分词检索能力**：
          前缀匹配只能命中"某个 token 以该词开头"，词中/词尾出现搜不到。

          现在两件事都变了：
            ① `innodb_ft_min_token_size = 1`（my.ini，重启生效）；
            ② `content_tokenized` 改用 unigram+bigram 机械切分（不再 jieba）。
          ⇒ 分词列**重新启用**，且支持单字与任意子串。

          即便某表的分词列尚未回填（MATCH 命中 0），也**不会静默降级**：
          本函数之外还有 LIKE 兜底路径（按日期倒序），草稿纸数据量小、付得起。

        ★ 表不同列名也不同：draftpad 与 dsh_compaction 都有 content_tokenized
          （前者 1802 行、后者 181 行，均 100% 回填）；若将来出现没有该列的
          表，必须显式加分支——写死列名会让该表全文路必然报 Unknown column，
          而异常若被吞掉就表现为「全文路无命中」（2026-09-22 实测抓到的静默降级）。
        """
        is_cjk = has_cjk(query)
        bo_query = build_cjk_boolean(query) if is_cjk else tokenize_query(query)
        if not bo_query:
            return []
        col = "content_tokenized" if table in ("draftpad", "dsh_compaction") else "summary"

        sql = (
            f"SELECT {select_cols}, MATCH({col}) AGAINST(? IN BOOLEAN MODE) AS _score "
            f"FROM {table} "
            f"WHERE MATCH({col}) AGAINST(? IN BOOLEAN MODE) {where_extra} "
            f"ORDER BY _score DESC LIMIT ?"
        )
        try:
            rows = self._db.query(sql, (bo_query, bo_query, *params_extra, limit),
                                  readonly=True)
        except Exception as exc:
            logger.warning("fulltext path failed (table=%s col=%s): %s",
                           table, col, exc)
            raise FulltextPathError(
                f"全文路失败（{table}.{col}）: {type(exc).__name__}: {exc}") from exc
        out: list[Hit] = []
        # 归一化：FULLTEXT 的 BM25 风味分**无上界**（实测一条 = 1.232），
        # 与向量路的余弦分 (0,1] 不是同一量纲 —— 混在同一个 sim 列里会误导。
        # 统一压到 (0,1]：本批最高分 = 1.0（注意只在**本批内**可比，跨批不可比）。
        pairs = [(r, float(r.get("_score") or 0.0)) for r in rows]
        top = max((s for _, s in pairs), default=0.0)
        for r, s in pairs:
            h = self._row_to_hit(r, table)
            h.score = h.raw_score = (s / top) if top > 0 else 0.0
            h.match_type = MODE_FULLTEXT
            out.append(h)
        return out

    def _keyword_search(
        self, query: str, table: str, select_cols: str,
        where_extra: str, params_extra: tuple, limit: int,
    ) -> list[Hit]:
        """LIKE 串联（多关键词 OR）—— 精确字面兜底，支持数值探针。"""
        kws = split_keywords(query)
        if not kws:
            return []
        if table == "draftpad":
            like_cols = ("content",)
        else:
            # dsh_compaction：**L3 防漏网层必须参与检索** ——
            # 实测：摘要正文是英文（`summary LIKE '%慢查询%'` = 0），
            # 中文细节词只可能出现在 L3（summary_excerpts / keywords）；
            # 若只搜 summary，"摘要没写、原文有"的词永远搜不到。
            like_cols = ("summary", "summary_excerpts", "keywords")
        clauses: list[str] = []
        params: list[Any] = []
        for kw in kws:
            esc = escape_like(kw)
            for c in like_cols:
                clauses.append(f"{c} LIKE ? ESCAPE '\\\\'")
                params.append(f"%{esc}%")
        # 也匹配 note_key（草稿纸）
        if table == "draftpad":
            for kw in kws:
                clauses.append("note_key LIKE ? ESCAPE '\\\\'")
                params.append(f"%{escape_like(kw)}%")

        sql = (f"SELECT {select_cols} FROM {table} "
               f"WHERE 1=1 {where_extra} AND ({' OR '.join(clauses)}) "
               f"ORDER BY id DESC LIMIT ?")
        # ★ 参数顺序必须与 SQL 中占位符出现顺序**严格一致**：
        #   WHERE 1=1 {where_extra(=session_id/tags 参数)} AND ({clauses(=LIKE 参数)})
        #   ⇒ params_extra 必须先于 params。
        #   （2026-09-22 实测抓到的 bug：顺序反了会让 session_id 收到 LIKE 模式串，
        #     表现为"明明有数据却 0 命中"，且只在同时用 bucket/tags 过滤时暴露）
        rows = self._db.query(sql, (*params_extra, *params, limit), readonly=True)
        out: list[Hit] = []
        for rank, r in enumerate(rows):
            h = self._row_to_hit(r, table)
            h.score = h.raw_score = 1.0 / (1.0 + rank)
            h.match_type = MODE_KEYWORD
            out.append(h)
        return out

    def _run_plain(self, sql: str, params: tuple, table: str) -> list[Hit]:
        # condition 路是**唯一带原样 SQL 片段**的路径 ⇒ 用只读会话（纵深防御）
        rows = self._db.query(sql, params, readonly=True)
        return [self._row_to_hit(r, table) for r in rows]

    def _clue_search(
        self, query: str, table: str, select_cols: str,
        where_extra: str, params_extra: tuple,
    ) -> tuple[list[Hit], list[str], dict]:
        """线索路：**逐线索检索 + IDF 过滤 + 集成**，返回 (集成后的排序, 口径说明)。

        ## 为什么需要单独一路（而不是把线索串成一条查询）

        本项目的查询形态是 LLM 手写的多线索串（`"激光 地月 通信 时延 RPC信标 双向"`）：

        · 串成 **AND**（本项目原实现，每词加 `+`）⇒ **coordination level mismatch**：
          要求全部线索同时命中，232 条真实查询里 **200 条整条全文路空手**；
        · 串成 **OR** ⇒ 空手降到 0，但「命中了几条线索」这个信号被文档长度稀释；
        · **逐线索检索 + 集成** ⇒ 同时保住「命中了哪几条线索」（各线索自己的排序）
          与「命中了几条线索」（跨线索累加）。

        ## 单线索用 AND 恰恰是对的

        `+词*` 对**单条线索**是正确语义（这个词必须出现）；错的只是"把 N 条独立线索
        拼成一条 AND 查询"。故本路每条线索各发一次全文查询（单线索过 tokenize_query）。

        ## IDF 过滤（三条判据，实测 combo 版最佳）

        1. 线索清洗后短于 `clue_min_chars` ⇒ 丢（`AU`/`de`/被切碎的单字母）；
        2. 该线索 0 命中 ⇒ 丢（含 BOOLEAN 语法报错被记为空的）；
        3. 命中数**达到 `clue_depth` 上限** ⇒ 视为 IDF 极低的高频线索（等于停用词）⇒ 丢。
           实测砍掉 59% 的线索（1227→499），线索集成的 MRR 0.4213→0.4584。

        实测（REPORT.md §八，232 条真实查询）：与 OR 全文路**并进**后
        hit@5 145→**151**、MRR 0.4398→**0.4653**、未进池 12→**11**。
        注意**并进是必要的**——把线索集成**替代** OR 路会掉 hit@5（146 vs 151）。
        """
        cfg = self._cfg.search
        clues = [c for c in split_clues(query) if len(c) >= cfg.clue_min_chars]
        notes: list[str] = []
        stats = {"total": 0, "kept": 0, "capped": 0, "empty": 0, "failed": 0}
        if not clues:
            return [], notes, stats
        if len(clues) > cfg.clue_max:
            notes.append(f"线索 {len(clues)} 条超过上限 {cfg.clue_max}，"
                         f"只取前 {cfg.clue_max} 条")
            clues = clues[: cfg.clue_max]

        kept: list[list[Hit]] = []
        dropped_high_df: list[str] = []
        dropped_empty: list[str] = []
        failed: list[str] = []
        for clue in clues:
            try:
                hits = self._fulltext_search(clue, table, select_cols,
                                             where_extra, params_extra,
                                             cfg.clue_depth)
            except FulltextPathError as exc:
                # 单条线索失败**不拖垮整路**，但要计数并说明（不静默）
                failed.append(clue)
                logger.warning("clue fulltext failed (clue=%r): %s", clue, exc)
                continue
            if not hits:
                dropped_empty.append(clue)
                continue
            if len(hits) >= cfg.clue_depth:
                dropped_high_df.append(clue)     # 顶到上限 ⇒ 区分度极低
                continue
            kept.append(hits)

        stats.update(total=len(clues), kept=len(kept),
                     capped=len(dropped_high_df), empty=len(dropped_empty),
                     failed=len(failed))
        if not kept:
            notes.append(
                f"线索路无可用线索（共 {len(clues)} 条：高频被滤 "
                f"{len(dropped_high_df)}、无命中 {len(dropped_empty)}、"
                f"失败 {len(failed)}）")
            return [], notes, stats
        if dropped_high_df or failed:
            notes.append(
                f"线索路滤除高频线索 {len(dropped_high_df)} 条"
                f"（{','.join(dropped_high_df[:5])}）"
                + (f"；{len(failed)} 条线索全文查询失败" if failed else ""))

        # 集成：跨线索 RRF 累加后**除以线索数**（取均值）——
        # 否则线索越多总分越高，会与向量路的权重失去可比性。
        scores: dict[int, float] = {}
        order_seen: list[int] = []
        best: dict[int, Hit] = {}
        k = cfg.rrf_k
        for hits in kept:
            for rank, h in enumerate(hits):
                if h.id not in scores:
                    scores[h.id] = 0.0
                    order_seen.append(h.id)
                    best[h.id] = h
                scores[h.id] += 1.0 / (k + rank + 1)
        n = float(len(kept))
        for hid in scores:
            scores[hid] /= n
        # 稳定排序：同分保持首次出现顺序（与 _merge 的口径一致）
        order_seen.sort(key=lambda hid: -scores[hid])

        out: list[Hit] = []
        for hid in order_seen:
            h = best[hid]
            h.score = h.raw_score = scores[hid]
            h.match_type = MODE_CLUE
            out.append(h)
        return out, notes, stats

    # ── 组装 ──

    @staticmethod
    def _merge(scored: dict[int, Hit], hits: list[Hit], weight: float,
               k: Optional[int] = None) -> None:
        """按排名加权并入（同名命中计 hybrid 并累加分数）。

        融合式：`add = weight / (k + rank + 1)` —— rank 是 0-based，故
        `k=0` 等价于最朴素的 `weight / (1 + rank)`（**本项目原实现**），
        业界默认 `k=60`。本语料实测**非单调、甜点 10**（config.search.rrf_k）。

        `k` 省略时取模块默认 `_DEFAULT_RRF_K`，仅为不改动外部调用方的兼容口；
        `Retriever.find()` 一律显式传 `cfg.search.rrf_k`。
        """
        kk = _DEFAULT_RRF_K if k is None else int(k)
        for rank, h in enumerate(hits):
            add = weight / (kk + rank + 1)
            cur = scored.get(h.id)
            if cur is None:
                h.score = add
                scored[h.id] = h
            else:
                cur.score += add
                # raw_score 取两路中较大者（保留「最像的那一路」的相似度）
                cur.raw_score = max(cur.raw_score, h.raw_score)
                if cur.match_type != h.match_type:
                    cur.match_type = "hybrid"

    def _query_health(self, meta: "FindMeta", hits: list[Hit]) -> dict:
        """查询信息量体检 —— **只报数，不自动改变行为**（见 FindMeta.uncertainty）。

        为什么要有它：本语料实测大量查询**本身就是模糊的**（用户对 12 条裁决样本的
        原话：「都太模糊了」「这种非常模糊的词感觉也不好说什么」）。此时返回一批
        "看起来正常但实际无从判断"的结果，就是一种静默降级——必须显式说出来。

        判据分两层，**故意如此** —— 而且这两层回答的是**两个不同的问题**：
          · `flags` —— **机理级**：全部线索都是高频词 / 有命中的路数 ≤2。
            触发率 20% / 27%（232 条实测）。它说的是「**查询本身信息量不足**」
            ⇒ 提示"补充更精确的线索"。
          · `soft_flags` —— **连续信号越阈值**（前5分离度 < cfg.search.sep5_low）：
            它说的是「**结果之间区分度低、排序不可靠**」——**不是**查询的问题，
            而是"这几条谁更相关其实分不出来" ⇒ 提示"建议自己看内容判断"。
            （用户对 12 条裁决样本的原话区分得很清楚：一类是"太模糊了"，
            另一类是"都差不多/排的问题不大"——后者正是这一层。）
          · `numbers` —— 原始数值，供调用方自行判读。
        阈值只用 p25 不用 p50：分离度是**连续分布、不是双峰**（p50=0.419；取它会让
        50% 的查询都收到提示 ⇒ 狼来了）。
        """
        # ★ 判据的适用范围必须按**模式**限定 —— 否则就是我今天警告了一整天的
        #   "跨场景移植阈值"。两条理由：
        #   ① (a) 硬判据讲的是"线索路没可用线索 / 路数太少"，只在**走过线索路**的
        #      模式（auto / clue）里成立；
        #   ② (b) 软判据的阈值 0.316 是按 **auto 的融合分**标定的（232 条实测 p25），
        #      **不能**套到单路向量分上——纯向量的分数分布完全不同形。
        #   故：非 auto 模式下不做阈值判定，只由服务层给"模式固有"的说明。
        mode = meta.mode_requested
        stats = meta.uncertainty.get("clue_stats") or {}
        flags: list[str] = []
        if mode in (MODE_AUTO, MODE_CLUE):
            if stats.get("total") and not stats.get("kept"):
                flags.append(f"全部 {stats['total']} 条线索都是高频词（无区分度）")
        paths_hit = sum(1 for v in meta.counts.values() if v)
        if mode in (MODE_AUTO, MODE_CLUE) and 0 < paths_hit <= 2:
            flags.append(f"只有 {paths_hit} 条检索路给出命中（各路结果互不重合）")

        numbers: dict[str, float] = {}
        scores = [h.score for h in hits]
        # ★ 「前5分离度」只在**确实有 5 条**时才算。
        #   曾经不足 5 条时把 sep5 记成 0.0 ⇒ 0.0 < 0.316 恒成立 ⇒
        #   任何命中 ≤4 条的查询都误报「结果区分度偏低」。
        #   那不是"区分不出来"，那是"结果本来就少"，是另一个信号 ——
        #   硬凑一个 0.0 去比阈值＝制造狼来了（活体验证抓到：
        #   rerank=false、只返回 3 条，却报"前5分离度 0.000"）。
        numbers["n_hits"] = len(scores)
        if len(scores) >= 5 and scores[0] > 0:
            numbers["sep5"] = round((scores[0] - scores[4]) / scores[0], 3)
            numbers["head"] = round(scores[0] / sum(scores[:5]), 3)

        soft: list[str] = []
        thr = self._cfg.search.sep5_low
        if mode == MODE_AUTO and "sep5" in numbers and numbers["sep5"] < thr:
            soft.append(f"前5分离度 {numbers['sep5']:.3f}（低于阈值 {thr}）"
                        f"——这几位谁更相关其实分不出来")
        return {"flags": flags, "soft_flags": soft, "numbers": numbers,
                "paths_hit": paths_hit, "sep5_low": thr}

    def _row_to_hit(self, row: dict, table: str) -> Hit:
        raw_tags = row.get("tags")
        tags: list[str] = []
        if isinstance(raw_tags, str) and raw_tags.strip():
            try:
                parsed = json.loads(raw_tags)
                if isinstance(parsed, list):
                    tags = [str(x) for x in parsed]
            except Exception:
                tags = [t for t in raw_tags.split(",") if t]
        return Hit(
            id=int(row.get("id") or 0),
            score=0.0,
            match_type="",
            table=table,
            session_id=str(row.get("session_id") or ""),
            note_key=str(row.get("note_key") or ""),
            content=str(row.get("content") or ""),
            tags=tags,
            created_at=row.get("created_at"),
            turn_no=int(row.get("turn_no") or 0),
            shadowed_count=int(row.get("shadowed_count") or 0),
            shadowed_tokens=int(row.get("shadowed_tokens") or 0),
        )

    def _build_filters(
        self, bucket: Optional[str], tags: Optional[list[str]]
    ) -> tuple[str, tuple, list[str]]:
        """构造附加 WHERE（bucket / tags 过滤）。"""
        sql = ""
        params: list[Any] = []
        notes: list[str] = []
        if bucket:
            sql += " AND session_id = ?"
            params.append(bucket)
        if tags:
            for t in tags:
                sql += " AND tags LIKE ? ESCAPE '\\\\'"
                params.append(f"%{escape_like(t)}%")
            notes.append(f"tags 过滤: {','.join(tags)}")
        return sql, tuple(params), notes
