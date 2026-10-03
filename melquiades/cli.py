"""
文件名: melquiades/cli.py
摘要: 命令行入口 —— 首扫(backfill) / 单轮收割 / 状态 / 建表。

用法:
  python -m melquiades.cli ensure-tables
  python -m melquiades.cli harvest [--backfill] [--limit N] [--frames N]
  python -m melquiades.cli status [--logs 10]

说明: 首扫（170MB / 71 文件量级）**不进 MCP 启动路径**，用 --backfill 手动跑一次。
依赖: config.py, dbcore.py, harvest/*
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from melquiades.config import Config, default_config_path
from melquiades.dbcore import MariaDB
from melquiades.harvest.runner import HarvestRunner
from melquiades.harvest.store import HarvestStore
from melquiades.backfill import (
    backfill_embeddings,
    backfill_tokenized,
    count_missing_embedding,
    count_missing_tokenized,
)
from melquiades.retrieval import Retriever
from melquiades.schema import ensure_all_vectors


def _build(config_path: str) -> tuple[Config, MariaDB, HarvestStore]:
    cfg = Config.load(config_path)
    db = MariaDB(cfg.mariadb)
    store = HarvestStore(db)
    return cfg, db, store


def cmd_ensure_tables(args) -> int:
    cfg, db, store = _build(args.config)
    ok, msg = db.ping()
    print(f"DB: {'OK' if ok else 'FAIL'} {msg}")
    if not ok:
        return 1
    tables = store.ensure_tables()
    print("已确保表:", ", ".join(tables))
    return 0


def cmd_harvest(args) -> int:
    cfg, db, store = _build(args.config)
    ok, msg = db.ping()
    if not ok:
        print(f"DB 不可用: {msg}", file=sys.stderr)
        return 1

    store.ensure_tables()
    runner = HarvestRunner(cfg, store)

    # 首次全量扫 vs 单轮增量
    max_files = args.limit if args.limit is not None else 0
    max_frames = args.frames if args.frames is not None else 0

    if args.backfill:
        stats = runner.run_once(
            "manual", max_files=max_files, max_frames_per_file=max_frames
        )
    else:
        stats = runner.run_once(
            "manual",
            max_files=max_files or cfg.dsh.startup_scan_dirs_limit,
            max_frames_per_file=max_frames,
        )

    print(json.dumps({
        "run_id": stats.run_id,
        "trigger": stats.trigger,
        "status": stats.status,
        "files_seen": stats.files_seen,
        "files_scanned": stats.files_scanned,
        "frames_read": stats.frames_read,
        "events_new": stats.events_new,
        "compactions_new": stats.compactions_new,
        "cursor_advanced": stats.cursor_advanced,
        "skipped_no_change": stats.skipped_no_change,
        "errors": stats.errors[:10],
    }, ensure_ascii=False, indent=2))
    return 0 if stats.status != "error" else 2


def _drift_scan(logs: list, scan_n: int) -> tuple[dict, int]:
    """扫最近 scan_n 个日志的**事件类型**，返回 ({未登记类型: 次数}, 实际扫描数)。

    为什么放在 status 里：这是"上游漂移"唯一的**对外出口**。改上游适配之前先跑一次
    status，看有没有未登记的事件类型，比"等线上发现内容变少"便宜得多（见 README §4）。
    """
    from melquiades.harvest.drift import unknown_event_types
    from melquiades.harvest.reader import read_new_lines, split_lines_to_events

    agg: dict = {}
    scanned = 0
    for path in logs[:scan_n]:
        res = read_new_lines(path, 0, 0)
        if res.error:
            print(f"              漂移扫描跳过 {path}: {res.error}")
            continue
        scanned += 1
        for t, c in unknown_event_types(split_lines_to_events(res.lines)).items():
            agg[t] = agg.get(t, 0) + c
    return agg, scanned


def cmd_status(args) -> int:
    cfg, db, store = _build(args.config)
    ok, msg = db.ping()
    print(f"DB          : {'OK' if ok else 'FAIL'} {msg}")
    if ok:
        priv = db.check_privileges()
        if priv.get("risky"):
            print(f"权限自检    : ⚠️ 持有本应用不需要的权限: {', '.join(priv['risky'])}")
            print(f"              建议: REVOKE {' ,'.join(priv['risky'])} ON `{cfg.mariadb.database}`.* "
                  f"FROM '<user>'@'<host>';")
        else:
            print("权限自检    : ✓ 无多余危险权限")
    print(f"DSH_HOME    : {cfg.dsh_home}")
    print(f"sessions    : {cfg.sessions_path} (存在: {cfg.sessions_path.is_dir()})")
    if not ok:
        return 1

    store.ensure_tables()
    runner = HarvestRunner(cfg, store)

    logs = runner.discover_logs()
    cursors = store.all_offsets()
    print(f"日志文件    : {len(logs)} 个（已建游标 {len(cursors)} 个）")

    # ── 上游漂移扫描（本项目的"升级后第一眼"）──
    drift_n = getattr(args, "drift_logs", 1)
    if logs and drift_n > 0:
        agg, scanned = _drift_scan(logs, drift_n)
        if agg:
            print(f"上游漂移    : ⚠️ 最近 {scanned} 个日志里出现 {len(agg)} 种**未登记**事件类型：")
            for t, c in sorted(agg.items(), key=lambda kv: -kv[1]):
                print(f"              {c:>6}  {t}")
            print("              说明: DSH 可能已升级。请核对 harvest/drift.py 的 "
                  "KNOWN_EVENT_TYPES，并补一条黄金样本（见 README §4.4）。")
        else:
            print(f"上游漂移    : ✓ 最近 {scanned} 个日志无未登记事件类型")

    total = db.scalar("SELECT COUNT(*) FROM dsh_compaction") or 0
    sessions = db.scalar("SELECT COUNT(DISTINCT dsh_session) FROM dsh_compaction") or 0
    sh_tok = db.scalar("SELECT COALESCE(SUM(shadowed_tokens),0) FROM dsh_compaction") or 0
    print(f"压缩档案    : {total} 条 / {sessions} 个会话 / 遮蔽合计 {sh_tok} tokens")

    rows = store.recent_logs(limit=args.logs)
    if rows:
        print(f"\n最近 {len(rows)} 次收割:")
        for r in rows:
            print(f"  [{r['status']:7s}] {str(r['started_at'])[:19]} "
                  f"{r['trigger_kind']:8s} files={r['files_scanned']}/{r['files_seen']} "
                  f"frames={r['frames_read']} comp={r['compactions_new']} "
                  f"adv={r['cursor_advanced']}"
                  + (f" err={str(r['error_text'])[:60]}" if r['error_text'] else ""))
    return 0


def cmd_backfill(args) -> int:
    """回填 content_tokenized（jieba 分词）与 embedding（BGE 向量）。"""
    cfg, db, store = _build(args.config)
    ok, msg = db.ping()
    if not ok:
        print(f"DB 不可用: {msg}", file=sys.stderr)
        return 1

    vec_status = ensure_all_vectors(db)
    print("向量列/索引:", json.dumps(vec_status, ensure_ascii=False))

    table = args.table
    out: dict[str, Any] = {}
    if args.what in ("tokenized", "all"):
        miss = count_missing_tokenized(db, table)
        print(f"[tokenized] 待回填 {miss} 行")
        out["tokenized"] = backfill_tokenized(db, table=table, limit=args.limit)

    if args.what in ("embedding", "all"):
        miss = count_missing_embedding(db, table)
        print(f"[embedding] 待回填 {miss} 行")
        rt = Retriever(cfg, db)
        # 各表正文列不同：draftpad.content / dsh_compaction.summary
        text_col = "content" if table == "draftpad" else "summary"
        out["embedding"] = backfill_embeddings(
            db, rt.embed, table=table, text_col=text_col, limit=args.limit
        )

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="melquiades.cli")
    p.add_argument("--config", default=str(default_config_path()),
                   help="配置文件路径（默认项目根 config.yaml）")
    p.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ensure-tables", help="只建新表（幂等）")
    sp.set_defaults(func=cmd_ensure_tables)

    sp = sub.add_parser("harvest", help="跑一轮收割")
    sp.add_argument("--backfill", action="store_true",
                    help="首扫模式：全量（受配置限制），不进启动路径")
    sp.add_argument("--limit", type=int, default=None, help="本轮最多处理几个文件")
    sp.add_argument("--frames", type=int, default=None, help="单文件本轮最多解压几帧")
    sp.set_defaults(func=cmd_harvest)

    sp = sub.add_parser("status", help="查看游标与最近收割记录")
    sp.add_argument("--logs", type=int, default=10)
    sp.add_argument("--drift-logs", type=int, default=1,
                    help="上游漂移扫描几个最新日志（0=跳过）")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("backfill", help="回填 tokenized / embedding")
    sp.add_argument("what", choices=["tokenized", "embedding", "all"])
    sp.add_argument("--table", default="draftpad")
    sp.add_argument("--limit", type=int, default=0, help="最多处理多少行（0=全部）")
    sp.set_defaults(func=cmd_backfill)

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
