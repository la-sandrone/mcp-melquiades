#!/usr/bin/env python3
"""v1 → v2 压缩档案回填：把 seq 指针译成 guid 指针。

## 为什么要有 v2（schema.sql 的原话）

    -- 6) 压缩档案指针 —— 与 seq 脱钩后的重建落点
    --    现 dsh_compaction 存的是 seq 指针，v3→v4 后会错位；
    --    本表改存 guid 范围，使 archive_fetch 不再依赖 seq。

v1 有两重依赖，都要断掉：
  ① seq 指针 —— 官方 v3→v4 迁移会重编号 seq（migration.ts: "only `seq` moves"）
  ② **源日志文件** —— 实测 17 份已消失且无备份；而 archive_fetch 现在读的正是它，
     源一没就 NOT_FOUND。v2 改从 dsh_message 取（我们自己抄下来的，自足）。

## 译法

译法本身在 melquiades/harvest/compaction_v2.py —— 那是唯一真相源，
收割流程（store.insert_compaction）用的是同一份，避免抄两份必然漂移。

    dsh_message 里同时有 seq 与 guid，天然是映射表：
        SELECT guid, line_no FROM dsh_message
        WHERE session_id=? AND source_gens=? AND seq IN (...)

★ 对不上的 seq 属**正常**（无正文块的控制事件本就不产 guid），按比例报告，不报错。

## 用法

    python3 tools/backfill_compaction_v2.py            # 干跑（默认，只报告）
    python3 tools/backfill_compaction_v2.py --apply    # 真写（先导出备份到 _backup/）
    python3 tools/backfill_compaction_v2.py --apply --replace
                                                       # 同时修正已存在的空指针记录

退出码: 0 成功 / 1 运行失败 / 2 前置拒绝
"""

import argparse
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from melquiades.config import Config, default_config_path          # noqa: E402
from melquiades.dbcore import MariaDB                              # noqa: E402
from melquiades.harvest.compaction_v2 import insert_v2_row, translate  # noqa: E402

V1_COLUMNS = (
    "id, dsh_session, compaction_id, turn_no, file_path,"
    " shadowed_count, shadowed_tokens, shadowed_seqs, summary"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写（缺省只干跑）")
    ap.add_argument("--replace", action="store_true",
                    help="用 REPLACE INTO（修正已存在的记录，如空指针）；缺省 INSERT IGNORE")
    args = ap.parse_args()

    cfg = Config.load(str(default_config_path()))
    db = MariaDB(cfg.mariadb)

    ok, msg = db.ping()
    if not ok:
        print(f"❌ DB 不可用: {msg}")
        return 2

    with db.cursor(dictionary=True) as cur:
        cur.execute(f"SELECT {V1_COLUMNS} FROM dsh_compaction ORDER BY id")
        v1_rows = cur.fetchall()

    print(f"v1 档案: {len(v1_rows)} 条")
    compiled = [translate(db, r) for r in v1_rows]
    ready = [c for c in compiled if "skip_reason" not in c["diag"]]
    skipped = [c for c in compiled if "skip_reason" in c["diag"]]

    full = [c for c in ready if c["diag"]["ratio"] >= 0.98]
    part = [c for c in ready if 0 < c["diag"]["ratio"] < 0.98]
    zero = [c for c in ready if c["diag"]["ratio"] == 0]
    total_guids = sum(len(c["shadowed_guids"]) for c in ready)

    print()
    print("可译性分布（对不上的 seq 属正常：无正文块的控制事件本就不产 guid）")
    print(f"  完全可译 ≥98% : {len(full)} 条")
    print(f"  部分可译       : {len(part)} 条")
    print(f"  零可译 0%      : {len(zero)} 条"
          f"   ← 多为「源日志+正文都已不在」的历史垃圾，属预期")
    print(f"  直接跳过       : {len(skipped)} 条（结构性不可译）")
    print(f"  合计 guid 指针 : {total_guids} 个")

    if skipped:
        print()
        print("跳过明细（前 8 条）:")
        for c in skipped[:8]:
            print(f"  id={c['diag'].get('v1_id')} {c['diag']['skip_reason']}")

    if zero:
        print()
        print("零可译明细（前 5 条）:")
        for c in zero[:5]:
            print(f"  id={c['diag'].get('v1_id')} {c['session_id'][:26]}"
                  f" gens={c['source_gens']} seqs={c['diag']['seqs_total']}")

    if not args.apply:
        print()
        print("[干跑结束] 加 --apply 真写。")
        print("  写入前会把 v1 相关行导出到 _backup/ 以便回滚。")
        return 0

    # ── 真写 ──
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = ROOT / "_backup"
    backup_dir.mkdir(exist_ok=True)
    backup_file = backup_dir / f"compaction_backfill_{stamp}.json"
    with backup_file.open("w", encoding="utf-8") as fh:
        json.dump(v1_rows, fh, ensure_ascii=False, default=str, indent=1)
    print()
    print(f"① 已备份 v1 原始行 → {backup_file.relative_to(ROOT)}")

    inserted = 0
    failed = 0
    for c in ready:
        try:
            insert_v2_row(db, c, replace=args.replace)
            inserted += 1
        except Exception as exc:
            failed += 1
            if failed <= 5:
                print(f"  ✗ id={c['diag'].get('v1_id')} 写入失败: {exc}")

    verb = "REPLACE" if args.replace else "INSERT IGNORE"
    print(f"② {verb} 入 dsh_compaction_v2: 成功 {inserted} 条，失败 {failed} 条")

    with db.cursor(dictionary=True) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM dsh_compaction_v2")
        n = cur.fetchone()["n"]
        cur.execute("""SELECT COUNT(*) AS n FROM dsh_compaction_v2
                       WHERE shadowed_guids IS NOT NULL AND shadowed_guids <> '[]'""")
        n_ptr = cur.fetchone()["n"]
    print(f"③ 回读校验: 表内 {n} 行，其中带非空 guid 指针的 {n_ptr} 行")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
