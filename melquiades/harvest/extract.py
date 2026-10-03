"""
文件名: melquiades/harvest/extract.py
摘要: compaction 事件 → 三层入库内容（L1 摘要 / L2 遮蔽 seq 指针 / L3 防漏网关键词）。
      纯逻辑，零数据库依赖，便于单测。

事件契约（2026-09-22 从 DSH live 日志实测，4 种类型）:

  compaction/prune    data: {shadowedRange:{start,end}, shadowedSeqs:[int], shadowedTokenCount:int}
                      ⚠️ 无 compactionId —— 关联靠**时序**（出现在 start 之前）
  compaction/start    data: {compactionId, turn}
  compaction/summary  data: {compactionId, summary:[ContentBlock]}   ← 摘要正文（ContentBlock 数组）
  compaction/end      data: {compactionId, turn}

关联策略: 一条 compaction 记录以 compactionId 为唯一键；
         其 shadowedSeqs / shadowedTokenCount 取自「上一个 end 之后、本 start 之前」的
         所有 prune 事件之并集。summary 到达即可落库（此刻数据已齐）。

依赖: 无（除标准库）；L3 关键词用本项目的 tokenizer（可选）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from melquiades.tokenizer import split_keywords

# 事件类型常量
EV_PRUNE = "compaction/prune"
EV_START = "compaction/start"
EV_SUMMARY = "compaction/summary"
EV_END = "compaction/end"

# 摘要正文的哨兵标记（DSH 自己写在摘要开头）
SUMMARY_MARKER = "<compacted-summary>"


@dataclass
class CompactionRecord:
    """一条待入库的压缩记录。"""

    compaction_id: str
    turn_no: Optional[int] = None
    occurred_at_ms: Optional[int] = None

    summary: str = ""
    """L1：摘要正文（纯文本）。"""

    shadowed_seqs: list[int] = field(default_factory=list)
    """L2：被遮蔽的事件 seq 列表（回查指针）。
    ★ 数据源优先级：compaction/summary 自带（实测 719 条，完整）
      > compaction/prune 累积（实测仅 3 条，逐条增量，仅作兜底）。"""

    shadowed_tokens: int = 0
    """被遮蔽的 token 数（DSH 自报）。"""

    shadowed_range_start: Optional[int] = None
    """被遮蔽的 surface 位置跨度起（注意是位置跨度，非 seq 区间）。"""

    shadowed_range_end: Optional[int] = None
    """被遮蔽的 surface 位置跨度止。"""

    provider: str = ""
    """做摘要的 provider（如 deepseek-official）。"""

    model: str = ""
    """做摘要的模型（如 deepseek-flash）。"""

    input_tokens: int = 0
    """摘要调用的输入 token（context rot 曲线用）。"""

    output_tokens: int = 0
    """摘要调用的输出 token。"""

    cache_read_tokens: int = 0
    """摘要调用的缓存命中 token。"""

    summary_seq: Optional[int] = None
    """summary 事件的 seq（便于回查原文）。"""

    keywords: str = ""
    """L3：防漏网关键词（jieba 切出的词，空格分隔）。"""

    summary_excerpts: str = ""
    """L3：被遮蔽事件的首段摘要（仅本次扫描可见范围内的事件）。"""

    @property
    def shadowed_count(self) -> int:
        return len(self.shadowed_seqs)


def _blocks_to_text(blocks: Any) -> str:
    """
    把 ContentBlock 数组拼接为纯文本。

    DSH 的 compaction/summary.data.summary 形如
      [{"type":"text","text":"<compacted-summary>..."}]
    非 text 类型（图/工具块）忽略；结构异常时尽力而为。
    """
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for blk in blocks:
        if isinstance(blk, dict):
            txt = blk.get("text")
            if isinstance(txt, str):
                parts.append(txt)
        elif isinstance(blk, str):
            parts.append(blk)
    return "\n".join(parts)


def build_excerpts(
    seq_index: dict[int, dict],
    shadowed_seqs: list[int],
    excerpt_chars: int,
    max_items: int,
) -> str:
    """
    L3 防漏网层：为被遮蔽的事件抽取「首段摘要」。

    动机: 摘要压缩比约 35:1（实测 719 条 / ~426k tokens → 12K 摘要），
          摘要里没写的词，在摘要里永远搜不到，而原文有。
          故额外收录被遮蔽事件的首段文本，作为检索词来源。

    实现: 按 shadowed_seqs 顺序查 seq_index（O(1) 查表）；
          不可见的 seq（在更早批次）自然跳过 —— 由 L2 指针承担回查职责。
    """
    if not shadowed_seqs or excerpt_chars <= 0 or max_items <= 0:
        return ""

    out: list[str] = []
    for seq in shadowed_seqs:
        ev = seq_index.get(seq)
        if ev is None:
            continue
        text = event_digest(ev, excerpt_chars)
        if text:
            out.append(f"#{seq} {text}")
        if len(out) >= max_items:
            break
    return "\n".join(out)


def event_digest(ev: dict, limit: int = 0) -> str:
    """
    把单个事件压成一行可读文本。

    两处复用：L3 防漏网层抽取；`archive_fetch` 按 seq 取回原文。
    limit=0 表示不截断。
    """
    t = ev.get("type", "")
    data = ev.get("data") or {}
    text = ""

    if t in ("user/message", "assistant/message"):
        # 消息正文可能在 data.text / data.message.content[*].content[*].text
        text = _find_text(data)
    elif t in ("tool/call",):
        name = data.get("name", "")
        args = data.get("arguments", "")
        text = f"tool={name} {args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)}"
    elif t in ("tool/result",):
        text = _find_text(data)
    else:
        return ""

    text = " ".join(str(text).split())
    if not text:
        return ""
    return text if limit <= 0 else text[:limit]


def _collect_texts(node: Any, out: list[str], depth: int = 0) -> None:
    """递归收集结构中**所有** `text` 字段（不短路）—— 供完整取回原文用。"""
    if depth > 10:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "text" and isinstance(v, str):
                out.append(v)
            elif isinstance(v, (dict, list)):
                _collect_texts(v, out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _collect_texts(item, out, depth + 1)


def event_full_text(ev: dict) -> str:
    """
    取回事件的**完整**文本（拼接全部 ContentBlock）。

    与 `event_digest` 的分工：
      · `event_digest` 只取**首段** —— 供 L3 防漏网层（要短、要省）
      · `event_full_text` **完整拼接** —— 供 archive_fetch / archive_grep

    为什么必须分开：`_find_text` 是「找到第一段就返回」，用于取原文会
    **静默丢内容**（实测：同一 seq，recall_fetch 返回的正文比
    只取首段的版本多出整张表格）。
    """
    t = ev.get("type", "")
    data = ev.get("data") or {}
    parts: list[str] = []

    if t in ("user/message", "assistant/message", "tool/result"):
        _collect_texts(data, parts)
    elif t == "tool/call":
        name = data.get("name", "")
        args = data.get("arguments", "")
        parts.append(f"tool={name} "
                     f"{args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)}")
    else:
        return ""

    seen: set[str] = set()
    uniq: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return "\n".join(uniq)


def _find_text(node: Any, depth: int = 0) -> str:
    """在嵌套结构中寻找第一段像正文的文本。"""
    if depth > 6:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        for key in ("text", "content", "message", "output"):
            if key in node:
                got = _find_text(node[key], depth + 1)
                if got:
                    return got
        return ""
    if isinstance(node, list):
        for item in node:
            got = _find_text(item, depth + 1)
            if got:
                return got
        return ""
    return ""


def extract_compactions(
    events: list[dict],
    *,
    excerpt_chars: int = 200,
    keywords_max: int = 400,
    shadowed_seqs_max: int = 20000,
    summary_max_chars: int = 200000,
    include_excerpts: bool = True,
) -> list[CompactionRecord]:
    """
    从事件流中提取压缩记录（含三层内容）。

    状态机:
      · prune            → 暂存 seq / token（等待归属到其后的 start）
      · start(compactionId) → 开一条记录，接管暂存的 prune 数据
      · summary(compactionId) → 填摘要 → **产出记录**（此刻数据齐）
      · end(compactionId)   → 清理

    跨批次安全: 只对本次 events 里「start 与 summary 都出现」的记录产出；
    未闭合的（summary 尚未来到）留待下次。
    """
    records: list[CompactionRecord] = []
    seq_index: dict[int, dict] = {
        ev["seq"]: ev for ev in events if isinstance(ev.get("seq"), int)
    }
    pending_seqs: list[int] = []
    pending_tokens: int = 0
    seen_pending: set[int] = set()
    open_recs: dict[str, CompactionRecord] = {}

    for ev in events:
        et = ev.get("type")
        if et == EV_PRUNE:
            data = ev.get("data") or {}
            seqs = data.get("shadowedSeqs") or []
            if isinstance(seqs, list):
                for s in seqs:
                    if isinstance(s, int) and s not in seen_pending:
                        seen_pending.add(s)
                        pending_seqs.append(s)
            tok = data.get("shadowedTokenCount")
            if isinstance(tok, int):
                pending_tokens += tok

        elif et == EV_START:
            data = ev.get("data") or {}
            cid = data.get("compactionId")
            if not isinstance(cid, str) or not cid:
                continue
            rec = CompactionRecord(
                compaction_id=cid,
                turn_no=data.get("turn") if isinstance(data.get("turn"), int) else None,
                occurred_at_ms=ev.get("time") if isinstance(ev.get("time"), int) else None,
                shadowed_seqs=list(pending_seqs)[:shadowed_seqs_max],
                shadowed_tokens=pending_tokens,
            )
            open_recs[cid] = rec
            # prune 已被本次 start 接管
            pending_seqs = []
            pending_tokens = 0
            seen_pending = set()

        elif et == EV_SUMMARY:
            data = ev.get("data") or {}
            cid = data.get("compactionId")
            if not isinstance(cid, str) or not cid:
                continue
            rec = open_recs.get(cid)
            if rec is None:
                # summary 先于 start 出现（不该发生）→ 仍建记录，避免丢数据
                rec = CompactionRecord(compaction_id=cid)
                open_recs[cid] = rec

            # ★★ L2 主源：summary 自带完整遮蔽清单（实测 719 条 vs prune 的 3 条）
            seqs = data.get("shadowedSeqs")
            if isinstance(seqs, list) and seqs:
                rec.shadowed_seqs = [
                    s for s in seqs if isinstance(s, int)
                ][:shadowed_seqs_max]
                tok = data.get("shadowedTokenCount")
                rec.shadowed_tokens = tok if isinstance(tok, int) else 0

            rng = data.get("shadowedRange")
            if isinstance(rng, dict):
                if isinstance(rng.get("start"), int):
                    rec.shadowed_range_start = rng["start"]
                if isinstance(rng.get("end"), int):
                    rec.shadowed_range_end = rng["end"]

            # 元数据（供 context rot 曲线与溯源）
            if isinstance(data.get("provider"), str):
                rec.provider = data["provider"]
            if isinstance(data.get("model"), str):
                rec.model = data["model"]
            usage = data.get("usage")
            if isinstance(usage, dict) and "inputTokens" in usage:
                rec.input_tokens = usage.get("inputTokens") or 0
                rec.output_tokens = usage.get("outputTokens") or 0
                rec.cache_read_tokens = usage.get("cacheReadTokens") or 0

            rec.summary = _blocks_to_text(data.get("summary"))[:summary_max_chars]
            if isinstance(ev.get("seq"), int):
                rec.summary_seq = ev.get("seq")

            if include_excerpts:
                rec.summary_excerpts = build_excerpts(
                    seq_index, rec.shadowed_seqs, excerpt_chars, max_items=keywords_max
                )
            # L3 关键词：从摘要 + 防漏网层抽取
            blob = rec.summary + "\n" + rec.summary_excerpts
            rec.keywords = " ".join(split_keywords(blob)[:keywords_max])

            records.append(rec)

        elif et == EV_END:
            data = ev.get("data") or {}
            cid = data.get("compactionId")
            if isinstance(cid, str):
                open_recs.pop(cid, None)

    return records
