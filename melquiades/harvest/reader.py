"""
文件名: melquiades/harvest/reader.py
摘要: DSH 会话日志读取 —— 多帧 zstd 逐帧解压 + 字节偏移游标（增量读取）。
      纯逻辑、零数据库依赖，便于单测。

设计依据（2026-09-22 实测）:
  · DSH 会话日志 = 多帧 zstd 追加流（单会话实测 180 帧 / 773,775 B → 解压 2,485,759 B）
  · **帧边界对齐行边界**（80 帧实测全部以 \\n 结尾）⇒ 无需跨帧行缓冲
  · 文件在会话进行中持续增长；活跃写入时 stream_reader 可正常读出
  · 最后一帧可能是半帧（正在写入）→ 解压失败即停在该帧之前，不推进 offset

依赖: zstandard
"""

from __future__ import annotations

import os
import zstandard as zstd
from dataclasses import dataclass, field


@dataclass
class TailReadResult:
    """一次增量读取的结果。"""

    lines: list[bytes] = field(default_factory=list)
    """本次新增的完整行（原始字节，已去空行）。"""

    new_offset: int = 0
    """推进后的字节偏移 —— **必定落在帧边界上**。"""

    frames_read: int = 0
    """本次解压的帧数。"""

    stopped_early: bool = False
    """是否因 max_frames 预算提前停止（true 时调用方应尽快再跑一轮）。"""

    truncated: bool = False
    """文件是否比记录的偏移短（被截断/重写）—— true 时已从头重读。"""

    error: str = ""
    """非空表示读取失败（文件不存在/权限/解压异常），lines 可能为空。"""


def read_new_lines(
    path: str,
    offset: int = 0,
    max_frames: int = 0,
) -> TailReadResult:
    """
    从 `offset` 起增量读取多帧 zstd 日志，返回新增行与新偏移。

    参数:
      path        — 日志文件路径
      offset      — 上次处理到的字节偏移（0 = 从头）
      max_frames  — 本次最多解压多少帧；0 = 不限（限速用）

    行为:
      · 逐帧解压，遇到半帧（ZstdError）立即停止，**offset 停在该帧之前**
      · offset 落在帧边界上 ⇒ 下次调用可安全 `seek(offset)` 续读
      · 文件比 offset 短（被重写）⇒ 自动从头重读并置 truncated=True
    """
    result = TailReadResult(new_offset=offset)

    try:
        size = os.path.getsize(path)
    except OSError as exc:
        result.error = f"stat failed: {exc}"
        return result

    start = offset
    if offset > size:
        # 文件被截断或重写 —— 从头来，并显式标注（不静默）
        start = 0
        result.truncated = True

    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            raw = fh.read()
    except OSError as exc:
        result.error = f"open/read failed: {exc}"
        return result

    pos = 0
    frames = 0
    while pos < len(raw):
        if max_frames and frames >= max_frames:
            result.stopped_early = True
            break

        dobj = zstd.ZstdDecompressor().decompressobj()
        try:
            data = dobj.decompress(raw[pos:])
        except zstd.ZstdError:
            # 半帧（正在写入）或损坏 —— 停在当前帧之前，offset 不前进到此处
            break

        unused = dobj.unused_data or b""
        consumed = len(raw) - pos - len(unused)
        if consumed <= 0:
            # 理论上不该发生；防御性退出，避免死循环
            break

        pos += consumed
        frames += 1

        if data:
            for line in data.split(b"\n"):
                if line.strip():
                    result.lines.append(line)

    result.frames_read = frames
    result.new_offset = start + pos
    return result


def split_lines_to_events(lines: list[bytes], max_parse_errors: int = 0) -> list[dict]:
    """
    把原始行解析为事件 dict，跳过损坏行。

    max_parse_errors=0 表示不限制（只计数）。返回解析成功的事件列表；
    解析失败计数通过返回值之外的 `last_parse_errors` 无法获取，
    故此处直接忽略坏行（调用方以 frames_read / lines 数量对比即可发现异常）。
    """
    import json

    events: list[dict] = []
    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def session_id_from_path(path: str) -> str:
    """
    从日志路径提取 DSH 会话 uuid。

    路径形如: <root>/--<workspace>--/session-<uuid>/session.v3.jsonl.zstd
    取 `session-<uuid>` 目录名中的 uuid 部分；取不到时返回空串。
    """
    try:
        parent = os.path.basename(os.path.dirname(path))
    except Exception:
        return ""
    if parent.startswith("session-"):
        return parent[len("session-"):]
    return parent
