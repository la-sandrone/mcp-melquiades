"""
文件名: melquiades/harvest/compaction_v2.py
摘要: v1(seq 指针) → v2(guid 指针) 的翻译逻辑 —— **共享给回填脚本与收割流程**。

为什么单独成模块
----------------
同一套译法有两个调用方：
  · tools/backfill_compaction_v2.py  一次性回填历史档案
  · harvest/store.py                 收割时同步写 v2
抄两份必然漂移，故提为唯一真相源。

核心译法
--------
dsh_message 里**同时有 seq 和 guid**，天然是映射表：

    SELECT guid, line_no FROM dsh_message
    WHERE session_id=? AND source_gens=? AND seq IN (...)

★ 不能用 dsh_message.is_shadowed 抄近路 —— 实测 4/4 样本该列全为 0
  （列建了但从未写入）。

★ 对不上的 seq 是**正常**的：没有正文块的事件（纯控制事件）本就不产 guid 行。
  故按比例报告，不报错。

★ 必须限定 source_gens：跨代际 seq 数值会撞车 —— 这正是 v1 的病根
  （官方 v3→v4 迁移会重编号 seq）。

三个坑（2026-09-26 实测踩过）
----------------------------
① dsh_compaction.dsh_session 是**裸 uuid(36)**，v2.session_id 是 CHAR(44)
   → 补 `session-` 前缀，否则 36/44 口径分裂。
② v1 **没有 source_gens** → 从 file_path 解析（session.v3.jsonl.zstd → 3）。
③ v1.compaction_id 是 varchar(64)，v2 是 char(36) → 超长**跳过并登记**，
   不截断（截断会让主键撞车）。

依赖: melquiades.dbcore
"""

from __future__ import annotations

import json
import re

GEN_RE = re.compile(r"session(?:\.v([1-9][0-9]*))?\.jsonl\.zstd$")

V2_COLUMNS = (
    "compaction_id", "session_id", "source_gens", "turn", "summary",
    "shadowed_count", "shadowed_tokens", "first_line_no", "last_line_no",
    "shadowed_guids",
)


def gens_of(path: str):
    """从日志路径解析代际；解析不出返回 None（不猜成 0）。"""
    m = GEN_RE.search(path or "")
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else 0


def session_id_44(raw: str) -> str:
    """把 dsh_session（可能是裸 uuid）规范成 CHAR(44) 的 `session-<uuid>`。"""
    sid = (raw or "").strip()
    if not sid:
        return ""
    return sid if sid.startswith("session-") else f"session-{sid}"


def translate(db, v1_row: dict, *, probe_msg: bool = True) -> dict:
    """把一条 v1 记录译成 v2 行。

    Args:
        db: MariaDB 实例。
        v1_row: dsh_compaction 的行（dict），需含 dsh_session / compaction_id /
                turn_no / file_path / shadowed_count / shadowed_tokens /
                shadowed_seqs / summary。
        probe_msg: True = 查 dsh_message 翻 guid；False = 只做结构校验与字段搬运
                   （收割时若正文侧尚未落库，用 False 先写空指针，事后回填补齐）。

    Returns:
        dict，含 v2 各列 + `diag`（诊断：skip_reason / seqs_total / guids_found / ratio）。
        `diag` 里有 `skip_reason` 表示不可译，调用方应跳过。
    """
    out = {
        "session_id": session_id_44(v1_row.get("dsh_session") or ""),
        "compaction_id": v1_row.get("compaction_id"),
        "source_gens": gens_of(v1_row.get("file_path") or ""),
        "turn": v1_row.get("turn_no"),
        "summary": v1_row.get("summary"),
        "shadowed_count": v1_row.get("shadowed_count") or 0,
        "shadowed_tokens": v1_row.get("shadowed_tokens") or 0,
        "first_line_no": None,
        "last_line_no": None,
        "shadowed_guids": [],
        "diag": {"v1_id": v1_row.get("id")},
    }

    if not out["session_id"]:
        out["diag"]["skip_reason"] = "dsh_session 为空"
        return out
    if out["source_gens"] is None:
        out["diag"]["skip_reason"] = f"file_path 解析不出代际: {v1_row.get('file_path')!r}"
        return out
    cid = out["compaction_id"] or ""
    if not cid:
        out["diag"]["skip_reason"] = "compaction_id 为空"
        return out
    if len(cid) > 36:
        out["diag"]["skip_reason"] = f"compaction_id 长 {len(cid)} > v2.char(36)，跳过（不截断）"
        return out

    try:
        seqs = json.loads(v1_row.get("shadowed_seqs") or "[]")
    except Exception:
        seqs = []
    out["diag"]["seqs_total"] = len(seqs)

    if not seqs:
        out["diag"]["skip_reason"] = "shadowed_seqs 为空"
        return out

    if not probe_msg:
        out["diag"]["guids_found"] = 0
        out["diag"]["ratio"] = 0.0
        out["diag"]["probe_skipped"] = True
        return out

    # 严格限定同一代际：跨代际 seq 会撞车（v1 的病根）
    placeholders = ",".join(["?"] * len(seqs))
    sql = (
        "SELECT guid, line_no FROM dsh_message "
        "WHERE session_id=? AND source_gens=? AND seq IN (%s)" % placeholders
    )
    with db.cursor(dictionary=True) as cur:
        cur.execute(sql, tuple([out["session_id"], out["source_gens"]]
                               + [int(s) for s in seqs]))
        hits = cur.fetchall()

    guids = [h["guid"] for h in hits]
    lines = [int(h["line_no"]) for h in hits if h.get("line_no") is not None]
    out["shadowed_guids"] = guids
    out["first_line_no"] = min(lines) if lines else None
    out["last_line_no"] = max(lines) if lines else None
    out["diag"]["guids_found"] = len(guids)
    out["diag"]["ratio"] = (len(guids) / len(seqs)) if seqs else 0.0
    # shadowed_count 用实际译出的条数覆盖 —— 让「计数」与「指针」自洽
    out["shadowed_count"] = len(guids)
    return out


def insert_v2_row(db, row: dict, *, replace: bool = False) -> bool:
    """把 translate() 的产物写进 dsh_compaction_v2。返回是否写入成功。

    replace=True 时用 REPLACE INTO（用于事后回填修正空指针）；
    否则 INSERT IGNORE（收割时幂等写入，已存在不覆盖）。
    """
    verb = "REPLACE INTO" if replace else "INSERT IGNORE INTO"
    sql = (
        f"{verb} dsh_compaction_v2 ("
        + ", ".join(V2_COLUMNS) +
        ") VALUES (" + ", ".join(["?"] * len(V2_COLUMNS)) + ")"
    )
    params = (
        row["compaction_id"], row["session_id"], row["source_gens"], row["turn"],
        row["summary"], row["shadowed_count"], row["shadowed_tokens"],
        row["first_line_no"], row["last_line_no"],
        json.dumps(row["shadowed_guids"], ensure_ascii=False),
    )
    with db.cursor(readonly=False) as cur:
        cur.execute(sql, params)
    return True
