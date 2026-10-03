"""
文件名: rerank.py
摘要: 跨编码器精排（BGE reranker v2-m3，Cohere /v1/rerank 协议）。
      在**融合序候选池**上重排；失败**显式降级**（保留融合序 + 记录原因上浮），
      绝不静默假装排过。
依赖: ./config
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from melquiades.config import SearchConfig

logger = logging.getLogger(__name__)


@dataclass
class RerankOutcome:
    """一次重排的结果。**失败与"没排"必须可区分**（铁律：显式降级）。"""

    order: list[int] = field(default_factory=list)  # 相对传入 docs 的下标，降序
    scores: list[float] = field(default_factory=list)
    ok: bool = False
    error: str = ""
    latency_ms: int = 0
    tokens: int = 0


class Reranker:
    """跨编码器精排客户端（多端点按序尝试）。

    为什么需要它（量测依据见 tools/replay/REPORT.md）：
      · 融合序是**双塔 + 词法**的产物，两塔各自打分、再按排名倒数相加（RRF），
        没有 query×doc 的交互项 ⇒ 排序精度有天花板。
      · 重排器是**单塔交互模型**，直接对 (query, doc) 打分，排序精度更高。
        行为标签实测（193 条 used 标签，池=30，指标看 top-5）：
          auto   hit@5 114 → 127（+13），MRR 0.4331 → 0.5271（+0.094）
          vector hit@5  98 → 116（+18），MRR 0.4281 → 0.5327（+0.105）
      · ★ 但它**一格都补不了召回**：未进池 28/45 在开关重排后完全不动 ——
        池外的东西它看不见。故重排只解决"排得准"，不解决"找得全"。
    """

    def __init__(self, cfg: SearchConfig) -> None:
        self._cfg = cfg
        self._endpoint_in_use = ""

    @property
    def enabled(self) -> bool:
        """配置层面的开关（不看对端是否可达）。"""
        return bool(self._cfg.rerank_enabled) and bool(self._cfg.rerank_model)

    def _bases(self) -> list[str]:
        # 重排端点**独立可配**：留空才回退到嵌入端点。
        # 绑死在一起的坏处（实测踩到）：把端点配错一个位，嵌入和重排一起挂，
        # 而"重排挂了、嵌入没挂"这种**单侧故障**恰恰是最需要能单独验证的形态。
        bases = [b for b in (self._cfg.rerank_endpoints or self._cfg.bge_endpoints) if b]
        # 成功过的端点优先 —— 避免每轮都在不可用端点上白等超时（同 embed 策略）
        if self._endpoint_in_use and self._endpoint_in_use in bases:
            bases.remove(self._endpoint_in_use)
            bases.insert(0, self._endpoint_in_use)
        return bases

    def score(self, query: str, docs: list[str]) -> RerankOutcome:
        """对 docs 逐条打分并返回降序下标。

        失败时 `ok=False` + `error` 有值，`order` 为**自然序**（调用方据此退回融合序）。
        """
        if not docs:
            return RerankOutcome(order=[], scores=[], ok=True)
        payload = {
            "model": self._cfg.rerank_model,
            "query": query,
            "documents": docs,
            "top_n": len(docs),
        }
        tried: list[str] = []
        t0 = time.perf_counter()
        for attempt in range(max(1, self._cfg.rerank_retries)):
            for base in self._bases():
                try:
                    with httpx.Client(
                        base_url=base.rstrip("/"),
                        timeout=httpx.Timeout(
                            self._cfg.rerank_timeout_sec,
                            connect=self._cfg.rerank_connect_timeout_sec,
                        ),
                    ) as cli:
                        resp = cli.post("/v1/rerank", json=payload)
                        if resp.status_code == 429:
                            # 令牌桶限流：退避后重试（不是错误，但也不能静默丢弃）
                            tried.append(f"{base}:429")
                            time.sleep(self._cfg.rerank_backoff_sec * (attempt + 1))
                            continue
                        resp.raise_for_status()
                        body = resp.json()
                    scores = [0.0] * len(docs)
                    for item in body.get("results", []):
                        idx = item.get("index")
                        if isinstance(idx, int) and 0 <= idx < len(scores):
                            scores[idx] = float(item.get("relevance_score") or 0.0)
                    self._endpoint_in_use = base
                    usage = body.get("meta", {}).get("tokens", {})
                    return RerankOutcome(
                        order=sorted(range(len(docs)), key=lambda i: -scores[i]),
                        scores=scores, ok=True,
                        latency_ms=int((time.perf_counter() - t0) * 1000),
                        tokens=int(usage.get("input_tokens") or 0),
                    )
                except Exception as exc:  # noqa: BLE001 —— 记录后继续下一端点
                    tried.append(f"{base}:{type(exc).__name__}:{exc}")
        err = "；".join(tried) or "无可用端点"
        logger.warning("rerank failed: %s", err)
        return RerankOutcome(order=list(range(len(docs))), scores=[],
                             ok=False, error=err,
                             latency_ms=int((time.perf_counter() - t0) * 1000))

    def fetch_texts(self, db, table: str, ids: list[int],
                    text_col: str, cap: int) -> dict[int, str]:
        """按 id 取**正文**（截到 rerank_doc_chars）。

        ★ 为什么不复用检索返回的 `Hit.content`：那是**展示口径**，
          两张表的 SELECT 都把正文 `LEFT(col, 400)`。draftpad 正文均长 1390、
          dsh_compaction 摘要均长 10665 ⇒ 拿 400 字喂重排器，
          等于**用 29%（草稿纸）/ 3.8%（档案）的文档做精排**。
        """
        if not ids:
            return {}
        # cap 由调用方按**表**给出（cfg.search.rerank_doc_chars[table]）；
        # 不在这里读配置 —— 这里拿不到表名与展示宽度的关系。
        cap = int(cap)
        placeholders = ",".join("?" for _ in ids)
        sql = (f"SELECT id, LEFT({text_col}, {cap}) AS txt FROM {table} "
               f"WHERE id IN ({placeholders})")
        rows = db.query(sql, tuple(ids), readonly=True)
        return {int(r["id"]): str(r.get("txt") or "") for r in rows}
