"""
文件名: melquiades/search.py
摘要: 搜索逻辑 — 全文搜索 + 向量搜索 加权合并。
      向量搜索通过 HTTP 调用 server_bge.py（localhost:9005/9006）。
      向量搜索不可用时透明降级为纯全文搜索。

      CJK 自适应：中文/日文/韩文查询时自动提升向量搜索权重（0.8），
      降低 FULLTEXT 权重（0.2），解决 MariaDB InnoDB FULLTEXT 对
      CJK 分词支持薄弱的问题（MariaDB 无 ngram parser）。
依赖: db.py, model.py, httpx
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import httpx

from melquiades.model import DraftpadNote, SearchResult

logger = logging.getLogger(__name__)


# ── CJK 检测 ──
# Unicode 范围:
#   CJK 统一汉字     U+4E00–U+9FFF
#   CJK 扩展 A       U+3400–U+4DBF
#   CJK 兼容汉字     U+F900–U+FAFF
#   CJK 标点         U+3000–U+303F
#   全角符号/假名/韩文 U+FF00–U+FFEF
_CJK_RE = re.compile(
    r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff'
    r'\u3000-\u303f\uff00-\uffef]'
)

# CJK 查询时的权重分配：向量搜索为主，FULLTEXT 为辅
# MariaDB InnoDB FULLTEXT 默认解析器对 CJK 几乎无效
_CJK_FTS_WEIGHT = 0.2
_CJK_VECTOR_WEIGHT = 0.8


def _has_cjk(text: str) -> bool:
    """检测文本是否包含 CJK 字符"""
    return bool(_CJK_RE.search(text))


class Searcher:
    """
    搜索器 — 全文搜索 + 向量搜索的加权融合。

    设计：
      - 全文搜索（FTS）：MariaDB FULLTEXT INDEX，精确关键词匹配。
      - 向量搜索（Vector）：BGE embedding → 余弦距离排序。
      - 融合策略：加权线性合并（config 控制权重）。
      - CJK 自适应：中文查询自动调高向量权重。
    """

    def __init__(
        self,
        db: "Database",  # 避免循环 import，运行时类型
        bge_endpoint: str = "http://localhost:9005",
        default_model: str = "BAAI/bge-large-zh-v1.5",
        embedding_dim: int = 1024,
        fts_weight: float = 0.5,
        vector_weight: float = 0.5,
        top_k: int = 5,
    ) -> None:
        self._db = db
        self._bge_endpoint = bge_endpoint.rstrip("/")
        self._default_model = default_model
        self._embedding_dim = embedding_dim
        self._fts_weight = fts_weight
        self._vector_weight = vector_weight
        self._top_k = top_k
        self._http_client: Optional[httpx.Client] = None

    # ── HTTP 客户端（懒初始化）──

    def _get_client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = httpx.Client(
                base_url=self._bge_endpoint,
                timeout=httpx.Timeout(30.0, connect=5.0),
            )
        return self._http_client

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    # ── 搜索入口 ──

    def search(
        self,
        query: str,
        session_id: str,
        top_k: Optional[int] = None,
        guid: Optional[str] = None,
    ) -> list[SearchResult]:
        """
        混合搜索入口。

        参数：
          query:     搜索关键词。
          session_id: 当前会话 ID（默认搜索范围）。
          top_k:     返回条数（默认 self._top_k）。
          guid:      指定会话 GUID（跨会话搜索，由用户显式提供）。

        返回: 合并后的 SearchResult 列表（按分数降序）。

        CJK 自适应：
          检测到查询包含中文/日文/韩文字符时，自动将向量搜索权重
          提升至 0.8、FULLTEXT 权重降至 0.2。
          原因：MariaDB InnoDB FULLTEXT 默认解析器（空白分词）对
          CJK 语言几乎无效——中文词语之间没有空格分隔。
        """
        k = top_k or self._top_k
        target_session = guid or session_id

        # CJK 自适应权重
        if _has_cjk(query):
            fts_w = _CJK_FTS_WEIGHT
            vec_w = _CJK_VECTOR_WEIGHT
            logger.debug(
                "CJK query detected, vector-heavy weights (FTS=%.1f, Vector=%.1f)",
                fts_w, vec_w,
            )
        else:
            fts_w = self._fts_weight
            vec_w = self._vector_weight

        # 1. 全文搜索（总是执行）
        fts_results = self._db.fulltext_search(
            query=query,
            session_id=target_session,
            top_k=k,
        )

        # 2. 尝试向量搜索（失败则降级）
        vector_results: list[DraftpadNote] = []
        try:
            vector_results = self._vector_search(
                query=query,
                session_id=target_session,
                top_k=k,
            )
        except Exception as exc:
            logger.warning("Vector search failed, fallback to FTS only: %s", exc)

        # 3. 合并结果（传入自适应权重）
        return self._merge_results(fts_results, vector_results, k, fts_w, vec_w)

    # ── 向量搜索 ──

    def _vector_search(
        self,
        query: str,
        session_id: str,
        top_k: int,
    ) -> list[DraftpadNote]:
        """
        向量搜索流程：
          1. 调用 BGE server 获取 query 的 embedding
          2. 用 VECTOR_DISTANCE 查询最近邻
          3. 返回 DraftpadNote 列表

        当前实现：通过 BGE 服务的 /v1/embeddings 获取 query embedding，
        然后用 MariaDB VECTOR 距离函数查询。
        """
        # 获取 query embedding
        query_emb = self._get_embedding(query)
        if query_emb is None:
            return []

        # 向量维度检查
        if len(query_emb) != self._embedding_dim:
            logger.warning(
                "Embedding dim mismatch: got %d, expected %d. Vector search skipped.",
                len(query_emb), self._embedding_dim,
            )
            return []

        # 用 MariaDB VEC_Dist 查询
        # embedding 列表需要转为 MariaDB VECTOR 字符串格式
        emb_json = json.dumps(query_emb)
        conn = self._db._get_conn()
        try:
            cur = conn.cursor(dictionary=True)
            # MariaDB 12.x VECTOR_DISTANCE 函数
            sql = (
                "SELECT dp.id, dp.session_id, dp.content, dp.tags, "
                "dp.model_name, dp.chunk_index, dp.created_at, dp.updated_at, dp.deleted_at, "
                "VEC_DISTANCE(dv.embedding, ?) AS vec_distance "
                "FROM draftpad dp "
                "JOIN draftpad_vectors dv ON dp.id = dv.draftpad_id "
                "WHERE dp.session_id=? AND dp.deleted_at IS NULL "
                "ORDER BY vec_distance ASC "
                "LIMIT ?"
            )
            cur.execute(sql, (emb_json, session_id, top_k))
            rows = cur.fetchall()

            results = []
            for row in rows:
                note = self._db._row_to_note(row)
                # 向量距离转分数（距离越小分数越高）
                dist = row.get("vec_distance", 1.0)
                score = 1.0 / (1.0 + float(dist))
                results.append(note)

            logger.debug("Vector search returned %d results", len(results))
            return results
        except Exception as exc:
            logger.warning("MariaDB VECTOR query failed: %s", exc)
            return []
        finally:
            conn.close()

    def _get_embedding(self, text: str) -> Optional[list[float]]:
        """
        调用本地 BGE server 获取文本的 embedding 向量。

        端点: POST /v1/embeddings
        请求: {"input": "...", "model": "BAAI/bge-large-zh-v1.5"}
        返回: {"data": [{"embedding": [...], "index": 0}], "model": "..."}
        """
        try:
            client = self._get_client()
            resp = client.post(
                "/v1/embeddings",
                json={
                    "input": text,
                    "model": self._default_model,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            emb = data["data"][0]["embedding"]
            return emb
        except Exception as exc:
            logger.warning("BGE embedding request failed: %s", exc)
            return None

    # ── 结果合并 ──

    @staticmethod
    def _merge_results(
        fts: list[DraftpadNote],
        vector: list[DraftpadNote],
        top_k: int,
        fts_weight: float = 0.5,
        vector_weight: float = 0.5,
    ) -> list[SearchResult]:
        """
        合并全文搜索和向量搜索的结果。

        策略：
          - 按 note.id 去重
          - FTS 结果分 = fts_weight / (1 + rank)
          - Vector 结果分 = vector_weight / (1 + rank)
          - 同时命中的分数相加（标记为 hybrid）
          - 按总分降序取 top_k

        权重由调用方根据 CJK 检测结果传入：
          非 CJK 查询 → fts=0.5, vector=0.5（config 默认值）
          CJK 查询    → fts=0.2, vector=0.8（自适应）
        """
        scored: dict[int, SearchResult] = {}

        # FTS 结果
        for rank, note in enumerate(fts):
            score = fts_weight / (1.0 + rank)
            scored[note.id] = SearchResult(
                note=note,
                score=score,
                match_type="fts",
            )

        # Vector 结果
        for rank, note in enumerate(vector):
            score = vector_weight / (1.0 + rank)
            if note.id in scored:
                # 混合命中，分数叠加
                existing = scored[note.id]
                existing.score += score
                existing.match_type = "hybrid"
            else:
                scored[note.id] = SearchResult(
                    note=note,
                    score=score,
                    match_type="vector",
                )

        # 按分数降序取 top_k
        sorted_results = sorted(
            scored.values(),
            key=lambda r: r.score,
            reverse=True,
        )
        return sorted_results[:top_k]
