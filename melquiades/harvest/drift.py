"""
文件名: melquiades/harvest/drift.py
摘要: DSH **事件类型漂移探测** —— 纯函数、零数据库依赖、零上游依赖。

为什么需要它
------------
melquiades 的收割/抄录是**按事件类型认内容**的（见 extract.py 与 message_extract.py）。
上游一旦改名一个承载正文的事件类型（例：`assistant/message` → `assistant/output`），
抽取会**静默少收**——不报错、不崩溃，只是内容变少。历史上本项目已经吃过两次
同类静默归零（home 硬编码、日志名写死），故这里把"识别不到的类型"显式暴露出来。

判据（不是拍脑袋）
------------------
`KNOWN_EVENT_TYPES` 是**实测**得到的：对 40 个真实会话日志（2026-10）做了事件类型
直方图，共 38 种。凡是不在这 38 种里的事件类型，就是"上游变了"的信号——
**新增**是升级，**改名**才是事故，但两者都值得人在升级后看一眼。

用法
----
    from melquiades.harvest.drift import unknown_event_types
    novel = unknown_event_types(events)      # {type: count}
    if novel:
        ...                                  # 记日志 / 提示，**不要静默**

★ 本模块**不**擅自判定"新增类型是好是坏"——它只报事实，由人裁决。
"""

from __future__ import annotations

from typing import Iterable

#: 实测的 DSH 顶层事件类型全集（40 个真实会话日志直方图，2026-10，共 38 种）。
#: 顺序按实测频次降序，便于对照。**只增不改**：新增即为漂移信号。
MEASURED_EVENT_TYPES = frozenset({
    "tool/result",
    "tool/call",
    "step/start",
    "step/end",
    "assistant/message",
    "session-log-deepseek/delivery-accepted",
    "agent/inbox/spliced",
    "user/message",
    "turn/start",
    "turn/end",
    "web/deepseek-search-llm-request",
    "compaction/prune",
    "assistant/attempt",
    "llm/retry",
    "llm/retry-started",
    "workspace/changes",
    "request/header",
    "session/end-seed",
    "session/title",
    "permission/preset",
    "sandbox/mode",
    "approval/policy",
    "model/selection",
    "compaction/start",
    "compaction/end",
    "session",
    "system/message",
    "request/context",
    "todo/write",
    "session/title-llm-request",
    "compaction/summary",
    "command/run",
    "command/done",
    "goal/change",
    "agent-preset/selected",
    "developer/message",
    "approval/asked",
    "approval/decided",
})

#: 已登记但**故意跳过**的事件类型（流式分片，内容全被装配正文覆盖）。
#: 出处：harvest/message_extract.py 的 SKIP_EXTRACT_TYPES（有实测依据）。
#: ★ 它们不出现在上面 40 个日志的**顶层**直方图里（多为嵌套/流内形态），
#:   但抽取路径会碰到，故一并登记为"已知"——不算漂移。
SKIPPED_EVENT_TYPES = frozenset({
    "assistant/chunk",
    "reasoning-chunks",
    "text-chunks",
    "tool-call-chunks",
})

#: 判定"已知"的全集 = 实测顶层类型 ∪ 已登记的分片家族。
KNOWN_EVENT_TYPES = MEASURED_EVENT_TYPES | SKIPPED_EVENT_TYPES

#: melquiades 会从中**抽取正文**的类型（抽取侧契约，见 extract.py 与 message_extract.py）。
#: 这些类型若在某次升级后**消失**，说明改名了 —— 比"出现新类型"更值得警惕。
CONTENT_EVENT_TYPES = frozenset({
    "user/message",
    "assistant/message",
    "system/message",
    "developer/message",
    "tool/call",
    "tool/result",
    "compaction/prune",
    "compaction/start",
    "compaction/summary",
    "compaction/end",
})


def _type_of(ev) -> str:
    if not isinstance(ev, dict):
        return "<bad-event>"
    t = ev.get("type")
    return t if isinstance(t, str) and t else "<no-type>"


def unknown_event_types(events: Iterable) -> dict:
    """统计**未见过**的事件类型 → {type: 出现次数}。

    只报事实：空字典 = 全部类型都在已知集内（上游形态未变）。
    """
    out: dict = {}
    for ev in events:
        t = _type_of(ev)
        if t not in KNOWN_EVENT_TYPES:
            out[t] = out.get(t, 0) + 1
    return out


def missing_content_types(events: Iterable) -> set:
    """报告**已知会承载正文、但本次一个都没见到**的类型。

    与 `unknown_event_types` 互补：那个报"多出来的"，这个报"少掉的"。
    少掉通常意味着上游把某个正文类型改名了 —— 正是静默少收的前兆。
    """
    seen = {_type_of(ev) for ev in events}
    return set(CONTENT_EVENT_TYPES) - seen
