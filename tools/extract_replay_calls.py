#!/usr/bin/env python3
"""从 DSH 会话导出（zip）里抽「回查类」工具调用，做成重放考核标准。

为什么要有它：金标 12 条是**我自己挑的**，而真正的考核应该来自**当年真实发生的回查**——
用户让它去找旧对话/旧笔记，它真去搜了、真捞到了东西。把这些调用原样重放，
就是在问一个可验证的问题：**同样的查询，今天还能不能捞到当年那批记录。**

判定口径（自洽式，不需要人工标注）：
  · 期望集 = 当年那一次调用的返回文本里出现的记录 id（草稿纸 id / 会话 guid）
  · 指标   = recall@k（top-k 覆盖了期望集多少）+ 首个期望命中的名次（MRR）
  · 只要期望集里的记录还在库里，这条就能跑

用法:
  python3 tools/extract_replay_calls.py                      # 扫默认目录，写 tools/bench/replay_golden.json
  python3 tools/extract_replay_calls.py --dir "<目录>" --out <文件> --limit 200
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import zipfile
from pathlib import Path

# ★ 源目录**故意不给默认值**：本脚本要进公开仓，写死某个人的语料路径等于把
#   作者机器的目录布局一起发布出去（导出闸门也会拦）。用 --dir 显式给。
DEFAULT_OUT = Path(__file__).resolve().parent / "bench" / "replay_golden.json"

# 回查类工具：它们的共同点是「用查询串或指针到旧数据里捞东西」
LOOKBACK_TOOLS = (
    "draftpad_find",
    "search_OMNIA",
    "archive_find",
    "archive_grep",
    "draftpad_scan",
    "message_context",
    "session_event_search",
)

# 脱敏：命中任一条就整条丢弃（宁缺毋滥 —— 重放件要能进公开仓）
SENSITIVE_PATTERNS = (
    r"[A-Za-z]:\\",            # Windows 绝对路径
    r"/mnt/[a-z]/",            # WSL 挂载路径
    r"/home/", r"/tmp/", r"/root/",
    r"(?i)password", r"(?i)passwd", r"(?i)secret", r"(?i)api[_-]?key",
    r"(?i)token", r"(?i)bearer",
    r"[\w.+-]+@[\w-]+\.[\w.]+",  # 邮箱
    r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",  # IP
)

ID_RE = re.compile(r"\[\d+\]\s*id=(\d+)")
GUID_RE = re.compile(r"\b([0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b")
SESS_RE = re.compile(r"session-([0-9a-f]{8})")


def _is_sensitive(text: str) -> bool:
    return any(re.search(p, text) for p in SENSITIVE_PATTERNS)


def _iter_events(zip_path: Path):
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.endswith(".jsonl") and n.count("/") == 0]
        if not names:
            return
        with zf.open(names[0]) as fh:
            for raw in fh:
                try:
                    yield json.loads(raw)
                except Exception:  # noqa: BLE001 —— 半行/坏行直接跳过
                    continue


def extract_from_zip(zip_path: Path) -> list[dict]:
    calls: dict[str, tuple[str, str]] = {}
    results: dict[str, str] = {}
    order: list[str] = []
    for ev in _iter_events(zip_path):
        data = ev.get("data") or {}
        msg = data.get("message") or {}
        etype = ev.get("type")
        if etype == "tool/call":
            cid = data.get("callId")
            if cid:
                calls[cid] = (str(data.get("name") or ""), str(data.get("arguments") or ""))
                order.append(cid)
        elif etype == "tool/result":
            cid = msg.get("toolCallId") or (msg.get("source") or {}).get("callId")
            content = msg.get("content")
            if isinstance(content, list):
                results[cid] = "".join(b.get("text", "") for b in content if isinstance(b, dict))
            elif content:
                results[str(cid)] = str(content)

    out: list[dict] = []
    for cid in order:
        name, args_raw = calls.get(cid, ("", ""))
        if not any(t in name for t in LOOKBACK_TOOLS):
            continue
        try:
            args = json.loads(args_raw) if args_raw else {}
        except Exception:  # noqa: BLE001
            continue
        query = str(args.get("query") or args.get("q") or "").strip()
        if not query or len(query) > 120:
            continue
        if _is_sensitive(args_raw):
            continue
        text = results.get(cid, "")
        if not text:
            continue
        ids = [int(m) for m in ID_RE.findall(text)]
        guids = GUID_RE.findall(text)
        sessions = sorted(set(SESS_RE.findall(text)))
        if not ids and not guids:
            continue          # 当年就没捞到东西 —— 不能当「期望」用
        out.append({
            "tool": name.split("__")[-1],
            "query": query,
            "mode": str(args.get("mode") or ""),
            "limit": int(args.get("limit") or 0),
            "expected_ids": sorted(set(ids))[:50],
            "expected_guids": sorted(set(guids))[:50],
            "expected_sessions": sessions[:8],
            "src_zip": zip_path.name,
            "call_id": cid,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="存放 DSH 会话导出 zip 的目录")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--limit", type=int, default=200, help="最多保留多少条（按查询去重后）")
    args = ap.parse_args()

    zips = sorted(Path(args.dir).glob("*.zip"))
    if not zips:
        print("没有 zip：", args.dir)
        return 2

    rows: list[dict] = []
    per_zip = collections.Counter()
    for z in zips:
        try:
            got = extract_from_zip(z)
        except Exception as exc:  # noqa: BLE001
            print("  跳过 %s: %s" % (z.name, exc))
            continue
        per_zip[z.name] = len(got)
        rows.extend(got)

    # 去重：同一条查询在多次会话里被反复问是很常见的，留 limit 最大的那次
    best: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["tool"], r["query"])
        cur = best.get(key)
        if cur is None or (len(r["expected_ids"]) + len(r["expected_guids"])) > (
                len(cur["expected_ids"]) + len(cur["expected_guids"])):
            best[key] = r
    items = sorted(best.values(), key=lambda r: -(len(r["expected_ids"]) + len(r["expected_guids"])))[: args.limit]

    payload = {
        "meta": {
            "source_dir": args.dir,
            "zips": len(zips),
            "raw_calls_with_hits": len(rows),
            "after_dedup": len(best),
            "kept": len(items),
            "note": "期望集 = 当年那次调用返回文本里的记录 id；脱敏规则见 SENSITIVE_PATTERNS",
        },
        "items": items,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("zip 数:", len(zips), "| 带命中结果的回查调用:", len(rows), "| 去重后:", len(best), "| 保留:", len(items))
    for name, n in per_zip.most_common():
        print("   %-52s %4d" % (name[:52], n))
    print("→", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
