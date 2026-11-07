#!/usr/bin/env python3
"""检索基准：金标查询 → @5/@10/@20/MRR + 耗时。不判对错，只出证据。

用法:
  python3 tools/bench_search.py --build     # 生成/校验 tools/bench/golden.json
  python3 tools/bench_search.py             # 现口径（A1/A2 之后）
  python3 tools/bench_search.py --legacy    # 2026-11-07 改造前口径（子串切分停词）

为什么带 --legacy：TODO-SEARCH-AUDIT.md A1/A2 动的是**召回**，必须能用同一把尺子
给出「改前 / 改后」两组数字。旧实现已从 tokenizer.py 删除，这里照审计文档原样内联
一份（子串替换 + 49 个单字停词），只用于对照，不许被生产代码 import。

★ 指标口径（与 DESIGN.md 2026-11-07 那一轮一致）
  · sources 只取 messages（金标目标都是会话正文）
  · modes 固定 vector + fulltext —— **不把 like 算进基准**，否则 A5 改触发条件时
    分数会跟着晃动，量到的就不是 A1/A2 的收益
  · 每条查询的目标是一个会话 id 前缀集合；名次 = 第一条落在目标会话里的命中位置
  · 0 命中记 MRR 0
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from melquiades import omnibus as omnibus_mod            # noqa: E402
from melquiades.config import Config                     # noqa: E402
from melquiades.dbcore import MariaDB                    # noqa: E402
from melquiades.omnibus import OmnibusSearcher           # noqa: E402
from melquiades.retrieval import Retriever               # noqa: E402
from melquiades.tokenizer import (                       # noqa: E402
    _WORD_ONLY_RE, iter_chunks, tokenize_cjk_ub_v2,
)

GOLDEN = ROOT / "tools" / "bench" / "golden.json"

# 2026-11-07 改造前留在表里的 40 个单字（A1 已移出）。--legacy 时补回去。
LEGACY_SINGLE = (
    "我你他它们的了吗呢吧啊呀哦嗯着过地得和与在是有把被让给对从到就也都还又再才"
    "很太更最真要会能说找看聊"
)


def legacy_build(query: str, stopwords=()) -> str:
    """改造前的查询表达式（子串替换切块 + 块内 bigram）。只作对照。"""
    s = query or ""
    for w in sorted([x for x in (stopwords or ()) if x], key=len, reverse=True):
        s = s.replace(w, "\x00")
    parts = [p for p in s.split("\x00") if p.strip()] or ([query] if query else [])
    out: list[str] = []
    for part in parts:
        for is_cjk, chunk in iter_chunks(part):
            if is_cjk:
                if len(chunk) == 1:
                    out.append(chunk)
                else:
                    out.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
            else:
                out.extend(_WORD_ONLY_RE.findall(chunk))
    deduped = list(dict.fromkeys(t for t in out if t))
    return " ".join(deduped) or tokenize_cjk_ub_v2(query)


def legacy_stopwords(sw: tuple) -> tuple:
    extra = tuple(ch for ch in LEGACY_SINGLE if ch not in sw)
    return tuple(sw) + extra


# ── 候选金标 ──
# 目标会话**不手写**：--build 用词去库里查，落在 1~6 个会话里的才算「稀有词」，
# 这些会话就是该条查询的目标集。手写会话 id 在 2026-11-07 那一轮失传过一次
# （DESIGN.md 只留了 35e1830e / b24095f7 两个前缀），这次改成可复算。
# 第二列是「判定词」：查询里真正决定召回的那个词。
CANDIDATES = [
    ("Lyfe", "Lyfe", "短关键词（DESIGN 记为开向量后略差）"),
    ("四支柱", "四支柱", "短关键词"),
    ("尘埃等离子体", "尘埃等离子体", "短关键词"),
    ("等离子体生命", "等离子体生命", "中长词"),
    ("Bartlett", "Bartlett", "拉丁短词"),
    ("硅基生命", "硅基生命", "短关键词"),
    ("Lyfe 四支柱是什么", "四支柱", "问句（DESIGN 记为仍失败）"),
    ("四支柱 是什么", "四支柱", "问句"),
    ("尘埃等离子体 生命 判定", "尘埃等离子体", "问句"),
    ("等离子体生命算不算生命", "等离子体生命", "问句（含停词 算不算）"),
    ("Bartlett 是不是 化学家", "Bartlett", "问句（含停词 是不是）"),
    ("硅基生命 可能 吗", "硅基生命", "问句"),
]


MAX_TARGET_SESSIONS = 6


def build_golden(cfg: Config, db: MariaDB) -> int:
    kept, dropped = [], []
    for query, term, note in CANDIDATES:
        rows = db.query(
            "SELECT session_id, COUNT(*) c FROM dsh_message WHERE body LIKE ? "
            "GROUP BY session_id ORDER BY c DESC",
            ("%" + term + "%",),
        )
        if not rows:
            dropped.append((query, "词在库里 0 命中"))
            continue
        if len(rows) > MAX_TARGET_SESSIONS:
            dropped.append((query, "词太常见（%d 个会话）" % len(rows)))
            continue
        kept.append({"query": query, "term": term, "note": note,
                     "targets": [r["session_id"] for r in rows]})
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
    print("kept %d / dropped %d -> %s" % (len(kept), len(dropped), GOLDEN))
    for item in kept:
        print("  keep %-22s %-12s targets=%d" % (item["query"], item["term"], len(item["targets"])))
    for query, why in dropped:
        print("  丢   %-22s %s" % (query, why))
    return 0


def run(cfg: Config, golden: list[dict], legacy: bool, limit: int = 20) -> int:
    db = MariaDB(cfg.mariadb)
    retriever = Retriever(cfg, db)
    if legacy:
        omnibus_mod.build_cjk_query_v2 = legacy_build
        OmnibusSearcher._stopwords = lambda self: legacy_stopwords(cfg.search.stopwords)
        OmnibusSearcher._legacy_marker = True
    searcher = OmnibusSearcher(retriever)

    a5 = a10 = a20 = 0
    mrr = 0.0
    total_ms = 0.0
    print("%-26s %6s %8s  %s" % ("query", "rank", "ms", "note"))
    for item in golden:
        t0 = time.time()
        res = searcher.search(item["query"], limit=limit, sources=["messages"],
                              modes=["vector", "fulltext"], per_source_limit=limit)
        ms = (time.time() - t0) * 1000
        total_ms += ms
        rank = 0
        for i, hit in enumerate(res.get("hits", []), 1):
            sid = (hit.meta or {}).get("session_id") or ""
            if any(sid.startswith(t) for t in item["targets"]):
                rank = i
                break
        if rank and rank <= 5:
            a5 += 1
        if rank and rank <= 10:
            a10 += 1
        if rank and rank <= 20:
            a20 += 1
        if rank:
            mrr += 1.0 / rank
        print("%-26s %6s %8.0f  %s" % (item["query"], rank or "-", ms, item["note"]))
    n = len(golden)
    print("-" * 66)
    print("builder=%s  N=%d  @5=%d  @10=%d  @20=%d  MRR=%.3f  平均 %.0f ms"
          % ("legacy" if legacy else "current", n, a5, a10, a20, mrr / max(1, n), total_ms / max(1, n)))
    return 0


# ── 精度基准（A1/A2 专用）──
# 会话命中率那套尺子对 A1/A2 不敏感：金标查询的关键词都不含停词字（四支柱/等离子体生命/
# 尘埃等离子体），改前改后名次一模一样。真正被误伤的是「词里含停词字」的那批词，
# 它们表现为**精度崩塌**：查「地质」时旧口径把停词 地 删掉、只剩单字「质」，
# 于是 top-20 全是任何含「质」的行（质量/物质/本质…）。
# 判据：precision@20 = top-20 里正文真的含该词的比例。
PRECISION_TERMS = [
    "地质", "太阳", "能量", "过程", "超过", "更新", "说明", "对比", "对应",
    "到达", "归还", "再生", "会话", "社会", "机会", "所有", "心得", "重要",
    "真实", "真空", "性能", "要求", "最近", "地球化学", "真空层",
]


def run_precision(cfg: Config, terms: list[str], legacy: bool, limit: int = 20) -> int:
    db = MariaDB(cfg.mariadb)
    retriever = Retriever(cfg, db)
    if legacy:
        omnibus_mod.build_cjk_query_v2 = legacy_build
        OmnibusSearcher._stopwords = lambda self: legacy_stopwords(cfg.search.stopwords)
    searcher = OmnibusSearcher(retriever)

    tot_hit = tot_n = 0
    print("%-12s %8s %10s  %s" % ("term", "prec@20", "query_expr", "miss"))
    for term in terms:
        expr = (legacy_build(term, legacy_stopwords(cfg.search.stopwords))
                if legacy else omnibus_mod.build_cjk_query_v2(term, tuple(cfg.search.stopwords)))
        res = searcher.search(term, limit=limit, sources=["messages"],
                              modes=["fulltext"], per_source_limit=limit)
        hits = res.get("hits", [])[:limit]
        guids = [h.record_id for h in hits]
        bodies: dict[str, str] = {}
        if guids:
            rows = db.query(
                "SELECT guid, body FROM dsh_message WHERE guid IN (%s)"
                % ",".join(["?"] * len(guids)), tuple(guids))
            bodies = {r["guid"]: (r["body"] or "") for r in rows}
        good = sum(1 for g in guids if term in bodies.get(g, ""))
        tot_hit += good
        tot_n += len(guids)
        print("%-12s %8s %10s  %s" % (term, "%d/%d" % (good, len(guids)), expr,
                                      "" if guids else "0 命中"))
    print("-" * 60)
    print("builder=%s  precision@20 合计 %d/%d = %.1f%%"
          % ("legacy" if legacy else "current", tot_hit, tot_n, 100.0 * tot_hit / max(1, tot_n)))
    return 0



# ── 重放考核（tools/extract_replay_calls.py 的产物）──
# 金标 12 条是我自己挑的；这一份是**当年真实发生的回查**，期望集 = 当年那批记录 id。
# 指标：recall@k（top-k 覆盖期望集多少）、命中率（recall>0 的比例）、首个命中的名次。
REPLAY_FILE = ROOT / "tools" / "bench" / "replay_golden.json"


def run_replay(cfg: Config, legacy: bool, limit: int = 20, max_items: int = 60,
               rerank=None) -> int:
    db = MariaDB(cfg.mariadb)
    retriever = Retriever(cfg, db)
    if legacy:
        omnibus_mod.build_cjk_query_v2 = legacy_build
        OmnibusSearcher._stopwords = lambda self: legacy_stopwords(cfg.search.stopwords)
    searcher = OmnibusSearcher(retriever)

    data = json.loads(REPLAY_FILE.read_text(encoding="utf-8"))
    items = data["items"][:max_items]

    # 存活过滤：期望集里的记录可能已被删（草稿纸软删 / 会话重抄）——那些不能算「没捞到」
    alive = []
    for it in items:
        ids = list(it.get("expected_ids") or [])
        if ids:
            qm = ",".join(["?"] * len(ids))
            alive_ids = {r["id"] for r in db.query(
                "SELECT id FROM draftpad WHERE id IN (" + qm + ")", tuple(ids), readonly=True)}
            it["expected_ids"] = sorted(alive_ids)
        guids = list(it.get("expected_guids") or [])
        if guids:
            qm = ",".join(["?"] * len(guids))
            alive_g = {r["guid"] for r in db.query(
                "SELECT guid FROM dsh_message WHERE guid IN (" + qm + ")", tuple(guids), readonly=True)}
            it["expected_guids"] = sorted(alive_g)
        if it["expected_ids"] or it["expected_guids"]:
            alive.append(it)

    rec_sum = 0.0
    hits = 0
    mrr = 0.0
    shown = 0
    print("%-34s %6s %8s  %s" % ("query", "recall", "rank", "tool"))
    for it in alive:
        expect = {str(i) for i in it["expected_ids"]} | set(it["expected_guids"])
        # ★ 按**当年调用的那条路**重放，别一律走 omnibus：
        #   草稿纸类的真实入口是 Retriever.find(table=draftpad)（retrieval.py），
        #   跨源统一检索才是 omnibus。用错路会把「工具换了」误读成「召回退步」。
        if it["tool"].startswith("draftpad"):
            n = min(int(it.get("limit") or 0) or 20, 30)
            fres = retriever.find(it["query"], mode=(it.get("mode") or "auto"),
                                  table="draftpad", limit=n, rerank=rerank)
            got = [str(h.id) for h in fres.hits][:limit]
        else:
            res = searcher.search(it["query"], limit=limit, rerank=rerank)
            got = [h.record_id for h in res.get("hits", [])][:limit]
        covered = len(set(got) & expect)
        rec = covered / max(1, len(expect))
        rank = 0
        for i, rid in enumerate(got, 1):
            if rid in expect:
                rank = i
                break
        rec_sum += rec
        if covered:
            hits += 1
        if rank:
            mrr += 1.0 / rank
        if shown < 40:
            print("%-34s %5.0f%% %8s  %s" % (it["query"][:34], 100 * rec, rank or "-", it["tool"]))
            shown += 1
    n = len(alive)
    print("-" * 62)
    print("builder=%s  N=%d  平均 recall@%d=%.3f  命中率=%.1f%%  MRR=%.3f"
          % ("legacy" if legacy else "current", n, limit, rec_sum / max(1, n),
             100.0 * hits / max(1, n), mrr / max(1, n)))
    return 0

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--build", action="store_true", help="重建并校验 golden.json")
    ap.add_argument("--legacy", action="store_true", help="用改造前的口径跑")
    ap.add_argument("--precision", action="store_true",
                    help="跑精度基准（A1/A2 专用：词里含停词字的那批词）")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--replay", action="store_true", help="跑重放考核（真实回查调用）")
    ap.add_argument("--items", type=int, default=60, help="重放条数上限")
    ap.add_argument("--no-rerank", action="store_true", help="关掉跨编码器精排")
    args = ap.parse_args()

    cfg = Config.load(args.config)
    db = MariaDB(cfg.mariadb)
    if args.build:
        return build_golden(cfg, db)
    if not GOLDEN.is_file():
        print("缺 %s，先跑 --build" % GOLDEN)
        return 2
    if args.replay:
        return run_replay(cfg, args.legacy, args.limit, args.items,
                          rerank=False if args.no_rerank else None)
    if args.precision:
        return run_precision(cfg, PRECISION_TERMS, args.legacy, args.limit)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return run(cfg, golden, args.legacy, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
