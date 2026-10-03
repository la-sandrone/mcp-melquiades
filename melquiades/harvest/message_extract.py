"""
文件名: melquiades/harvest/message_extract.py
摘要: DSH 会话日志 → dsh_message / dsh_attachment 行的抽取规则（纯函数、零 DB 依赖）。

与 tools/dsh-transcribe/dshlog.py 同源 —— 那边的规则全部有实测依据，此处是同一条
规则的 MCP 侧副本（本包不能反向 import 工作区里的脚本）。

★ 规则要点（每条都有实测出处，见 tools/dsh-transcribe 的注释）：
  · 排除重复转储路径 stream / rawOutput —— 实测 100% 被装配正文覆盖
  · 跳过 chunk 家族（assistant/chunk 等 4 个）—— 实测为流式分片，内容全重复
  · guid = sha256(session_id ‖ 0x1F ‖ gens ‖ 0x1F ‖ line_no ‖ 0x1F ‖ block_path)
    **不含 seq** —— v3→v4 迁移会重编号 seq（官方 migration.ts: "only `seq` moves"）
  · 附件：递归找带 attachmentId 的 dict（不硬编码落点，四处落点一网打尽）
"""

from __future__ import annotations

import hashlib
import json
import re

BLOCK_KINDS = {"text", "reasoning", "image", "file", "tool-call", "tool-result"}

# 实测确认的重复转储路径（stream 只提 usage，不进正文；rawOutput 与 summary 同内容）
_DUP_PATH = re.compile(r"(?:^|/)(?:stream|rawOutput)/\d+(?:/|$)")

# 实测为「装配正文之重复」的 OOV 事件家族：跳过 + 登记，不抽取
SKIP_EXTRACT_TYPES = frozenset({
    "assistant/chunk",
    "reasoning-chunks",
    "text-chunks",
    "tool-call-chunks",
})

ATTACHMENT_ID_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


def ev_time_of(value):
    """事件 `time` → 'YYYY-MM-DD HH:MM:SS.ffffff'（本地时区）。

    ★ 实测：DSH 事件的 time 是 **epoch 毫秒整数**（例 1789785653163），不是 ISO 串。
      早前按"字符串"处理 ⇒ 全表 ev_time 落成 NULL。此处与 harvest/store.py
      的 `_ms_to_dt` 保持同一约定（本地时区、微秒精度）。
      取不到或不合法 ⇒ None（**不猜、不用 0 顶替**）。
    """
    import datetime as _dt

    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return _dt.datetime.fromtimestamp(value / 1000.0).strftime("%Y-%m-%d %H:%M:%S.%f")
    except (OSError, OverflowError, ValueError):
        return None


def guid_of(session_id: str, gens: int, line_no: int, block_path: str) -> str:
    """身份锚：物理坐标哈希，与 seq 无关。"""
    payload = "\x1f".join((session_id, str(gens), str(line_no), block_path))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def collect_blocks(node, path: str, out: list) -> None:
    """递归收集正文块 (path, kind, text)。"""
    if isinstance(node, dict):
        btype = node.get("type")
        if isinstance(btype, str) and btype in BLOCK_KINDS:
            for key in ("text", "reasoning", "content"):
                v = node.get(key)
                if isinstance(v, str) and v:
                    out.append((f"{path}/{key}", btype, v))
                    break
        for k, v in node.items():
            collect_blocks(v, f"{path}/{k}", out)
    elif isinstance(node, list):
        for idx, v in enumerate(node):
            collect_blocks(v, f"{path}/{idx}", out)


def event_blocks(etype: str, data) -> tuple[list[tuple[str, str, str]], int]:
    """取正文块。返回 (块列表, 被排除的重复块数)。调用方必须把排除数计入统计。"""
    if etype in SKIP_EXTRACT_TYPES:
        return [], 0
    raw: list[tuple[str, str, str]] = []
    collect_blocks(data, "data", raw)
    out = [(p, k, t) for p, k, t in raw if not _DUP_PATH.search(p)]
    ndup = len(raw) - len(out)
    if etype == "tool/call" and isinstance(data, dict):
        args = data.get("arguments")
        if isinstance(args, str) and args:
            out.append(("data/arguments", "tool-arg", args))
        elif args is not None:
            out.append(("data/arguments", "tool-arg", json.dumps(args, ensure_ascii=False)))
    return out, ndup


def collect_attachments(node, path: str, out: list) -> None:
    """递归收集附件引用（按 attachmentId 判定，避开工具参数名的噪音）。"""
    if isinstance(node, dict):
        aid = node.get("attachmentId")
        if isinstance(aid, str):
            out.append({
                "block_path": path[:160],
                "attachment_id": aid,
                "sha256": aid[7:] if aid.startswith("sha256:") else None,
                "bytes": node.get("bytes"),
                "width": node.get("width"),
                "height": node.get("height"),
                "media_type": node.get("mediaType"),
                "name": node.get("name"),
            })
        for k, v in node.items():
            collect_attachments(v, f"{path}/{k}", out)
    elif isinstance(node, list):
        for idx, v in enumerate(node):
            collect_attachments(v, f"{path}/{idx}", out)


def messages_from_events(
    events: list[dict],
    session_id: str,
    gens: int,
    line_base: int,
) -> tuple[list[dict], list[dict], dict]:
    """
    把一批事件转成待入库的行。

    参数:
      events     — 事件 dict 列表（**已按行号顺序**）
      session_id — header.id（形如 session-<uuid>，44 字符）
      gens       — 日志代数
      line_base  — 本批第一个事件的**解压后行号**（与全量抄录同一坐标系）

    返回: (message_rows, attachment_rows, stats)
      stats = {dup_skipped, events, bad_attachment_ids}
    """
    rows: list[dict] = []
    atts: list[dict] = []
    dup_skipped = 0
    events_seen = 0
    bad_ids: list[str] = []

    for offset, obj in enumerate(events):
        # ★ 坏行/空行在增量读里以 None 占位，**下标必须保留**（它就是行号）。
        #   不判 None 会直接 AttributeError —— 而那正是"坏一行毁整轮"。
        if not isinstance(obj, dict):
            continue
        etype = obj.get("type")
        if not isinstance(etype, str):
            continue
        events_seen += 1
        line_no = line_base + offset

        # 附件引用：所有事件都扫（落点分散，挑类型必漏）
        before = len(atts)
        collect_attachments(obj.get("data"), "data", atts)
        for a in atts[before:]:
            a["line_no"] = line_no
            if not ATTACHMENT_ID_RE.match(a["attachment_id"]):
                bad_ids.append(f"line {line_no}: {a['attachment_id']!r}")

        blocks, ndup = event_blocks(etype, obj.get("data"))
        dup_skipped += ndup
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        msg = data.get("message") if isinstance(data.get("message"), dict) else {}
        for ordinal, (bpath, kind, text) in enumerate(blocks):
            nb = len(text.encode("utf-8"))
            rows.append({
                "guid": guid_of(session_id, gens, line_no, bpath),
                "line_no": line_no,
                "block_path": bpath,
                "ordinal": ordinal,
                "seq": obj.get("seq"),
                "ev_type": etype,
                "kind": kind,
                "role": msg.get("role"),
                "turn": data.get("turn"),
                "ev_time": ev_time_of(obj.get("time")),
                "body": text,
                "body_bytes": nb,
                "body_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            })

    return rows, atts, {
        "dup_skipped": dup_skipped,
        "events": events_seen,
        "bad_attachment_ids": bad_ids,
    }
