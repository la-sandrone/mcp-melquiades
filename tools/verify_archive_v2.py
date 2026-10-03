#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件名: verify_archive_v2.py
摘要: 端到端验证 archive/draftpad 修复（P1/P2/P10/P11）。
      不经过 MCP 进程（它加载的仍是旧代码），直接用新代码调 Retriever。
依赖: melquiades.{config, dbcore, retrieval}
用法: python3 tools/verify_archive_v2.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from melquiades.config import Config, default_config_path
from melquiades.dbcore import MariaDB
from melquiades.retrieval import Retriever

SID = "bdf722ae-e022-4a14-9787-c21cc66bd43d"


def show(tag, res, n=3):
    print("  [%s] %s" % (tag, res.explain()))
    for h in res.hits[:n]:
        print("      id=%-4s score=%.4f sim=%.4f 路=%-9s 会话=%s turn=%s 遮蔽=%s条/%stok" % (
            h.id, h.score, h.raw_score, h.match_type, (h.session_id or "-")[:12],
            h.turn_no, h.shadowed_count, h.shadowed_tokens))
    if not res.hits:
        print("      （无命中）")


def main():
    cfg = Config.load(str(default_config_path()))
    db = MariaDB(cfg.mariadb)
    rt = Retriever(cfg, db)

    print("=== 0. embedded 分布（零向量应已被标记排除）===")
    for t in ("draftpad", "dsh_compaction"):
        n1 = db.scalar("SELECT COUNT(*) FROM %s WHERE embedded=1" % t)
        n0 = db.scalar("SELECT COUNT(*) FROM %s WHERE embedded=0" % t)
        print("  %-16s embedded=1: %s  embedded=0: %s" % (t, n1, n0))

    print("\n=== 1. P11a 向量路（score 不应再全是 1.0 = 零向量假高分）===")
    show("vector", rt.find("压缩 摘要 上下文恢复", mode="vector",
                           table="dsh_compaction", limit=5))

    print("\n=== 2. P10 全文路（dsh_compaction 应不再报 Unknown column）===")
    show("fulltext", rt.find("压缩", mode="fulltext", table="dsh_compaction", limit=3))

    print("\n=== 3. P2 condition 自动切换（query 空 ⇒ 列举本会话档案）===")
    show("auto+cond", rt.find("", mode="auto", table="dsh_compaction",
                              condition="dsh_session = ?",
                              condition_params=[SID], limit=5))

    print("\n=== 4. P2 condition 并入检索（query 非空）===")
    show("query+cond", rt.find("压缩", mode="auto", table="dsh_compaction",
                               condition="turn_no > ?", condition_params=[0], limit=3))

    print("\n=== 5. P1 compaction 量级字段是否带出 ===")
    r = rt.find("摘要", mode="vector", table="dsh_compaction", limit=1)
    if r.hits:
        h = r.hits[0]
        print("  id=%s turn_no=%s shadowed_count=%s shadowed_tokens=%s" % (
            h.id, h.turn_no, h.shadowed_count, h.shadowed_tokens))

    print("\n=== 6. 草稿纸侧未受影响 ===")
    show("draftpad", rt.find("排查 静默降级", mode="auto", table="draftpad", limit=3))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
