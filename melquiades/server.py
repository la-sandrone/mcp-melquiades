"""
文件名: melquiades/server.py
摘要: MCP 服务入口 —— 7 个工具，分两组：
      · 草稿纸（跨对话账本）: draftpad_write / scan / amend / find / tags
      · 压缩档案（DSH 压缩收割）: archive_find / archive_expand / archive_status

      与 v1 的差别（设计依据见 DESIGN.md）：
        ✗ 去掉 进程级 UUID 会话隔离 / 随机 note_key / switch_session / get_session_id
        ✗ 去掉 draftpad_set（UPSERT 并入 write 的 key 参数）
        ✗ 去掉 draftpad_delete / draftpad_log（审计表从未被使用：0 行）
        ✓ 新增 游标读(scan) / 批量追加(amend) / 多路检索(find) / tags 枚举
        ✓ 新增 压缩档案检索（L1 摘要 + L2 seq 指针 + L3 防漏网层）
        ✓ 启动时: 建表 + 向量列 + 启动收割调度器（三触发：定时器/搭车/启动扫）

依赖: config, dbcore, retrieval, backfill, schema, harvest/*
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Optional

# ───── mcp 1.x / 2.x 兼容：FastMCP 改名（由 tools/mcp2_migrate.py 生成）─────
# mcp 2.x 把 FastMCP 改名为 MCPServer（构造参数与 .tool()/.run() 均兼容）。
try:
    from mcp.server.fastmcp import FastMCP  # mcp 1.x
except ModuleNotFoundError:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
# ───── 兼容层结束 ─────

from melquiades.config import Config, default_config_path
from melquiades.dbcore import MariaDB
from melquiades.retrieval import (
    MODE_AUTO, MODE_CONDITION, MODE_FULLTEXT, MODE_KEYWORD, MODE_VECTOR,
    Retriever,
)
from melquiades.schema import ensure_all_vectors
from melquiades.sqlguard import COLS_COMPACTION, COLS_DRAFTPAD, SqlGuardError
from melquiades.tokenizer import tokenize
from melquiades.omnibus import (
    DEFAULT_MODES as OMNIA_DEFAULT_MODES,
    DEFAULT_SOURCES as OMNIA_DEFAULT_SOURCES,
    OmnibusSearcher,
    format_hits as omnia_format,
)
from melquiades.harvest.runner import HarvestRunner
from melquiades.harvest.scheduler import HarvestScheduler, spawn_fire_and_forget
from melquiades.harvest.store import HarvestStore

logger = logging.getLogger(__name__)

# ── 全局状态（main 初始化）──
_cfg: Optional[Config] = None
_db: Optional[MariaDB] = None
_retriever: Optional[Retriever] = None
_store: Optional[HarvestStore] = None
_runner: Optional[HarvestRunner] = None
_bridge: Optional[object] = None   # HTTP 桥的 HTTPServer 句柄（见 _start_http_bridge）

# ── 词表（SKOS 同义词环）缓存 ──
# 写入路径每次都要拿"现有标签"来比近邻，而全量标签要从 1000+ 行 draftpad 现算。
# 故加 TTL 缓存；**TTL 到期后重新加载**，不做"只在进程启动时加载一次"
# （否则写入了新标签却看不见，用户会以为提示坏了）。
VOCAB_CACHE_TTL_SEC = 120.0
_vocab_cache: dict[str, object] = {"at": 0.0, "vocab": None, "index": None}


def _vocab_and_index():
    """取 (Vocabulary, TagIndex)，带 TTL 缓存。失败时返回 (None, None) —— 调用方降级。"""
    import time as _t
    from melquiades.vocab import TagIndex, Vocabulary
    now = _t.monotonic()
    if (_vocab_cache["index"] is not None
            and now - float(_vocab_cache["at"] or 0) < VOCAB_CACHE_TTL_SEC):
        return _vocab_cache["vocab"], _vocab_cache["index"]
    try:
        assert _db is not None
        _vocab_cache["vocab"] = Vocabulary.load(_db)
        _vocab_cache["index"] = TagIndex.from_db(_db)
        _vocab_cache["at"] = now
    except Exception as exc:  # noqa: BLE001 —— 词表坏了不能拖垮检索/写入
        logger.error("词表加载失败（本次跳过提示）：%s", exc)
        return None, None
    return _vocab_cache["vocab"], _vocab_cache["index"]
_scheduler: Optional[HarvestScheduler] = None

mcp = FastMCP("melquiades")

# 列清单一律取自 sqlguard 的唯一真相源（整串白名单匹配，禁止手写副本）
_DRAFTPAD_COLS = COLS_DRAFTPAD
_COMPACTION_COLS = COLS_COMPACTION


# ──────────────────────────────────────────────
# 内部辅助
# ──────────────────────────────────────────────

def _bucket() -> str:
    assert _cfg is not None
    return _cfg.draftpad.default_bucket


def _current_session_id() -> str:
    """当前会话 id —— **剥掉日志目录的 session- 前缀**。

    库里 dsh_compaction.dsh_session 存的是**裸 uuid**（harvest/reader.py
    有意剥前缀），而 $DSH_SESSION_ID 是 `session-<uuid>`；直接混用会
    静默 0 命中（2026-09-22 实测踩到）。
    """
    raw = os.environ.get("DSH_SESSION_ID", "").strip()
    if not raw:
        return ""
    return raw[len("session-"):] if raw.startswith("session-") else raw


def _piggyback() -> None:
    """工具搭车：后台扫一轮（fire-and-forget，不阻塞响应）。"""
    if _cfg is None or _runner is None or not _cfg.dsh.harvest_enabled:
        return
    spawn_fire_and_forget(_runner.tool_scan)


# ── 相对时间的显示档位（命名常量，非裸数字）──
AGE_JUST_NOW_MIN = 5          # 小于此分钟数 → 「刚刚」
AGE_SEC_PER_MIN = 60
AGE_MIN_PER_HOUR = 60
AGE_SEC_PER_DAY = 86400
AGE_DAYS_PER_MONTH = 30       # 显示用近似值，不做日历精确
AGE_DAYS_PER_YEAR = 365


def _human_age(created_at: Any) -> str:
    """把创建时间折成相对时间（`（14天前）`）。空值/不可解析 → 空串。

    为什么值得算这一行：本语料的域内规则是「**新设定覆盖旧设定**」，
    判读新旧时要比较时间。LLM 不擅长日期心算——与其让它拿两个 ISO 时间戳
    做减法，不如把相对时间直接算好给它（同「需要数值计算就别让 LLM 心算」）。
    ★ 只做**显示**，不参与排序、不参与过滤——排序加近因先验实测无益
    （见 tools/replay/REPORT.md §8.3），且"新覆盖旧"近乎铁律但非铁律，
    该由 LLM 判读而非系统静默替换。
    """
    if not created_at:
        return ""
    try:
        then = datetime.fromisoformat(str(created_at)[:19])
    except (ValueError, TypeError):
        return ""
    seconds = (datetime.now() - then).total_seconds()
    if seconds < 0:
        return "（未来时间）"
    if seconds < AGE_JUST_NOW_MIN * AGE_MIN_PER_HOUR:
        return "（刚刚）"
    if seconds < AGE_MIN_PER_HOUR * AGE_SEC_PER_MIN:
        # ★ 必须有这一档：否则 5~59 分钟会落进「小时前」分支、算出「0小时前」
        #   （2026-09-22 活体验证抓到：draftpad_scan 显示「（0小时前）」）
        return f"（{int(seconds // AGE_SEC_PER_MIN)}分钟前）"
    if seconds < AGE_SEC_PER_DAY:
        return f"（{int(seconds // (AGE_MIN_PER_HOUR * AGE_SEC_PER_MIN))}小时前）"
    days = int(seconds // AGE_SEC_PER_DAY)
    if days < AGE_DAYS_PER_MONTH:
        return f"（{days}天前）"
    if days < AGE_DAYS_PER_YEAR:
        return f"（{max(1, days // AGE_DAYS_PER_MONTH)}个月前）"
    return f"（{days // AGE_DAYS_PER_YEAR}年前）"


def _uncertainty_hint(res) -> str:
    """查询信息量不足时的**显式提示**（不是自动拒答，也不是静默降级）。

    依据：搜索的本质是用信息消除不确定性；信息不够时正确做法是
    「挖掘新的隐含信息，**不妨再问问用户**」，而不是在关键词上玩数字和公式
    （吴军《数学之美》）。IR 里的正式名字是 Query Performance Prediction /
    query clarity —— **预检索**阶段就该判断该查询够不够确定。

    只有**机理级判据**才触发提示（离散、可解释、实测触发率 20%/27%）；
    连续信号（分离度/集中度）实测是连续分布、不是双峰，**只报数不判定**。
    返回空串表示不提示。
    """
    u = getattr(res.meta, "uncertainty", None) or {}
    mode = getattr(res.meta, "mode_requested", "")
    if mode == MODE_VECTOR:
        # 模糊/语义模式：提示要**改成它该说的话**。
        # 对"我就是想不起来"的调用方说"请补充更精确的线索"是反讽——
        # 它本来就给不出更精确的东西。这里要如实说明"命中是语义猜的、可能不精确，
        # 请核对内容；若要更强的字面约束可切 auto"。
        return ("⚠️ 语义模式（mode=vector）：靠向量隐式关联匹配，**命中可能不精确**——"
                "它找的是「语义上像」而不是「字面上有」。\n"
                "   请逐条核对内容；需要更字面的约束时切 mode=auto（会并上全文/线索路）。")
    flags = u.get("flags") or []
    soft = u.get("soft_flags") or []
    if not flags and not soft:
        return ""
    nums = u.get("numbers") or {}
    # ★ 只印**实际存在**的指标。曾经用 nums.get('sep5', 0) —— 缺值被补成 0
    #   照样印出来，于是「命中不足 5 条、分离度根本没算」被读成「分离度 0.00」。
    #   缺值显示为 0 就是编数据（与 sep5 记 0.0 触发误报是同一族）。
    bits: list[str] = []
    if "sep5" in nums:
        bits.append(f"前5分离度 {nums['sep5']:.2f}")
    if "head" in nums:
        bits.append(f"头部集中度 {nums['head']:.2f}")
    if "sep5" not in nums and "n_hits" in nums:
        bits.append(f"命中 {nums['n_hits']} 条（不足 5 条，分离度不可算）")
    ref = f"（参考：{'、'.join(bits)}）" if bits else ""
    out: list[str] = []
    if flags:
        out.append("⚠️ 查询信息量偏低：" + "；".join(flags) + "。")
        out.append("   结果可能不准确。**建议补充更精确的线索**（专名 / 数值 / 日期）"
                   "或换个更具体的词后重查；也可先用 keyword 路做字面核对。" + ref)
    if soft:
        out.append("⚠️ 结果区分度偏低：" + "；".join(soft) + "。")
        out.append("   排序不可靠不等于没有相关结果——**建议直接读下方内容判断**，"
                   "或补充更具体的线索缩小范围。")
    return "\n".join(out)


def _fmt_hits(res, limit_note: str = "", kind: str = "draftpad") -> str:
    """把 FindResult 格式化为人类可读文本（含口径说明）。"""
    if res.empty:
        return f"无结果（{res.explain()}）\n说明：三路皆空不代表'不存在'，可能是未嵌入/未分词。"
    lines = [f"命中 {len(res.hits)} 条 | {res.explain()}"]
    if limit_note:
        lines.append(limit_note)
    hint = _uncertainty_hint(res)
    if hint:
        lines.append(hint)
    if getattr(res.meta, "rerank", {}).get("applied"):
        # 重排后逐条 score 不再单调 —— 不说明的话会被读成 bug
        lines.append("（顺序已按 rr 分精排；score 是**融合分**、rr 是 (query×doc) 交互分，"
                     "两者语义不同，故 score 不随序号递减）")
    for i, h in enumerate(res.hits, 1):
        when = (f"{str(h.created_at)[:19]}{_human_age(h.created_at)}"
                if h.created_at else "-")
        # rr 只在**确实参与了重排**时才显示：池外的行 rr_score 无意义，
        # 拿 0.000 展示会被读成"重排给它打了 0 分"（score 是融合分，不是它）
        rr = (f" rr={h.rr_score:.3f}" if getattr(h, "reranked", False) else "")
        if kind == "compaction":
            lines.append(
                f"\n[{i}] id={h.id} score={h.score:.3f} sim={h.raw_score:.3f}{rr} "
                f"路={h.match_type} 会话={h.session_id or '-'} turn={h.turn_no} "
                f"遮蔽={h.shadowed_count}条/{h.shadowed_tokens}tok 时间={when}"
            )
        else:
            tags = ",".join(h.tags) if h.tags else "-"
            lines.append(
                f"\n[{i}] id={h.id} score={h.score:.3f} sim={h.raw_score:.3f}{rr} "
                f"路={h.match_type} bucket={h.session_id or '-'} "
                f"tags={tags} 时间={when}"
            )
            if h.note_key:
                lines.append(f"    key: {h.note_key[:100]}")
        lines.append(f"    {h.content[:300]}")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# 草稿纸（账本）
# ──────────────────────────────────────────────

# 注意：这两个是**内部辅助**，必须放在 @mcp.tool 装饰器**之前**。
# 曾经把它们插在装饰器与 def draftpad_write 之间 ⇒ 装饰器挂到了
# _query_expansion 上，draftpad_write 反而**不再是 MCP 工具**
# （表现为客户端里写入工具叫 `_query_expansion`）。
def _query_expansion(query: str, enabled: Optional[bool]) -> tuple[list[str], str]:
    """按受控词表给查询并入词，返回 (扩展词, **说明来源的 note**)。

    依据 ISO 25964-2 §24.3.2：搜索词表支持沿 broader/narrower 与同义词扩展。
    等价扩展与层级扩展**语义不同**（后者会把结果泛化），故说明必须精确指出
    各自来自哪条路 —— 由本层给出，因为只有这里知道来源。
    """
    assert _cfg is not None
    on = bool(_cfg.search.vocab_expand_query) if enabled is None else bool(enabled)
    if not on or not query.strip():
        return [], ""
    vocab, _ = _vocab_and_index()
    if vocab is None or vocab.concept_count == 0:
        return [], ""
    from melquiades.vocab import expand_query_terms
    extra, matched, detail = expand_query_terms(
        vocab, query, hierarchy=bool(_cfg.search.vocab_expand_hierarchy))
    if not extra:
        return [], ""
    src: list[str] = []
    if detail["equiv"]:
        src.append(f"同义 {len(detail['equiv'])} 个（{'、'.join(detail['equiv'][:4])}）")
    if detail["narrower"]:
        src.append(f"层级下位 {len(detail['narrower'])} 个"
                   f"（{'、'.join(detail['narrower'][:4])}）⇒ 结果会比原查询更宽")
    if detail["broader"]:
        src.append(f"层级上位 {len(detail['broader'])} 个"
                   f"（{'、'.join(detail['broader'][:4])}）⇒ 结果会比原查询更宽")
    note = "；".join(src) + f"（命中查询词：{'、'.join(matched)}）"
    logger.info("查询扩展 %r：%s", query, note)
    return extra, note


def _tag_hints(tags: Optional[list[str]]) -> str:
    """写入侧**标签近邻提示**（不阻断写入；词表不可用时静默返回空并记日志）。

    依据（三方汇聚）：
      · ISO 25964-2 §13.2.2(d)：精确等价可自动，其余需**人介入**
      · Zubiaga 2012 实测：系统**有没有标签建议**，会造出分布不同的 folksonomy
      · obsidian-ai-tag-curator 实践：优先复用已有标签；**同时列出未选中的近似候选**
      · 用户的判据：同样的信息，**写入时问比事后问便宜一个数量级**
    语义：**只提示，不阻止；只解释，不代决。**
    """
    if not tags:
        return ""
    vocab, index = _vocab_and_index()
    if index is None:
        return ""
    from melquiades.vocab import expand_query_terms, format_suggestions, suggest_labels
    submitted = {str(x).strip() for x in tags if str(x).strip()}
    blocks: list[str] = []
    for t in tags:
        t = str(t).strip()
        if not t:
            continue
        s = suggest_labels(t, index)
        if s:
            # 同批提交的其它标签要传进去：若候选就在本批里，说明两个词你本来就都写了，
            # 提示冗余（实测"5 个标签弹 4 条"的主因之一）。
            blocks.append(format_suggestions(t, s, also_submitted=submitted - {t}))
    return ("\n\n" + "\n".join(blocks)) if blocks else ""


@mcp.tool(
    description=(
        "写入一条笔记到草稿纸（跨对话项目账本）。\n\n"
        "参数:\n"
        "  content: 笔记正文（建议原子事实/数字/状态/中间结论）。\n"
        "  tags:    可选标签列表，如 ['口径修正','焦面关系']。\n"
        "  key:     可选话题槽。给了 key 则**同 key 覆盖**（UPSERT），\n"
        "           适合'同话题最新状态'；不给则**追加**新记录（推荐）。\n\n"
        "返回: 新记录 id。"
    )
)
def draftpad_write(content: str, tags: Optional[list[str]] = None,
                   key: Optional[str] = None) -> str:
    assert _cfg is not None and _db is not None
    if not content or not content.strip():
        return "ERROR: content 不能为空"

    text = content.strip()[: _cfg.draftpad.content_max_chars]
    tok = tokenize(text)
    tag_json = json.dumps(tags or [], ensure_ascii=False)
    note_key = (key or uuid.uuid4().hex[:12]).strip()[: _cfg.draftpad.note_key_max_chars]
    bucket = _bucket()

    try:
        if key:
            # UPSERT：同 bucket+key 覆盖（UNIQUE(session_id, note_key)）
            existing = _db.query_one(
                "SELECT id FROM draftpad WHERE session_id=? AND note_key=? "
                "AND deleted_at IS NULL", (bucket, note_key))
            if existing:
                # 内容变了 ⇒ 旧向量作废：把 embedded 打回 0 等补嵌，
                # 否则它会以「已嵌入」的身份被检索到**旧语义**（P15）。
                _db.execute(
                    "UPDATE draftpad SET content=?, content_tokenized=?, tags=?, "
                    "embedded=0 WHERE id=?", (text, tok, tag_json, existing["id"]))
                _piggyback()
                return (f"OK: 覆盖 id={existing['id']} (key={note_key})"
                        + _tag_hints(tags))
        # embedded=0：写入路径不算向量，交给收割定时器的补嵌环节
        n, new_id = _db.execute(
            "INSERT INTO draftpad (session_id, note_key, content, content_tokenized, "
            "tags, embedded) VALUES (?,?,?,?,?, 0)",
            (bucket, note_key, text, tok, tag_json))
        _piggyback()
        return (f"OK: id={new_id} (bucket={bucket}, key={note_key})"
                + _tag_hints(tags))
    except Exception as exc:
        logger.error("draftpad_write failed: %s", exc)
        return f"ERROR: {type(exc).__name__}: {exc}"


@mcp.tool(
    description=(
        "按游标顺序读账本（增量/浏览用，比搜索更省 token）。\n\n"
        "参数:\n"
        "  from_id: 起始 id（省略则从最新往前）。\n"
        "  width:   每条取多少字符（默认 400）。\n"
        "  limit:   最多几条（默认 50）。\n"
        "  ascending: true=正序(from_id 向后)，false=倒序取最新（默认）。"
    )
)
def draftpad_scan(from_id: Optional[int] = None, width: int = 0,
                  limit: int = 0, ascending: bool = False) -> str:
    assert _cfg is not None and _db is not None
    w = int(width or _cfg.draftpad.scan_default_width)
    n = int(limit or _cfg.draftpad.scan_default_limit)
    n = max(1, min(n, _cfg.draftpad.scan_max_limit))

    params: list[Any] = []
    where = "deleted_at IS NULL"
    if from_id is not None:
        where += " AND id >= ?" if ascending else " AND id <= ?"
        params.append(int(from_id))
    order = "ASC" if ascending else "DESC"

    rows = _db.query(
        f"SELECT id, note_key, LEFT(content, {w}) AS head, created_at, tags "
        f"FROM draftpad WHERE {where} ORDER BY id {order} LIMIT ?",
        (*params, n))
    if not rows:
        return "无记录"
    lines = [f"共 {len(rows)} 条（width={w}, {order}）"]
    for r in rows:
        lines.append(
            f"\nid={r['id']} [{str(r['created_at'])[:19]}{_human_age(r['created_at'])}]"
            f" key={str(r['note_key'])[:60]}")
        lines.append(f"  {str(r['head'])[:w]}")
    _piggyback()
    return "\n".join(lines)


@mcp.tool(
    description=(
        "批量追加修订（**不改原文**，在末尾追加指针段）—— 本项目的主力修订法。\n\n"
        "参数:\n"
        "  ids:      要标注的记录 id 列表（逗号分隔或数组）。\n"
        "  text:     追加内容（通常形如 '【口径·日期 定案】…权威见 idN'）。\n\n"
        "语义: content = CONCAT(content, '\\n\\n──────\\n' + text)，事务内批量执行。\n"
        "返回: 实际影响行数。"
    )
)
def draftpad_amend(ids: Any, text: str) -> str:
    assert _db is not None
    if not text or not text.strip():
        return "ERROR: text 不能为空"
    if isinstance(ids, str):
        id_list = [int(x) for x in ids.replace("，", ",").split(",") if x.strip()]
    elif isinstance(ids, (list, tuple)):
        id_list = [int(x) for x in ids]
    else:
        id_list = [int(ids)]
    if not id_list:
        return "ERROR: ids 为空"

    marker = "\n\n──────\n" + text.strip()
    affected = 0
    errors: list[str] = []
    for rid in id_list:
        try:
            # 追加同样改变了内容 ⇒ 也要把 embedded 打回 0（P15）
            n, _ = _db.execute(
                "UPDATE draftpad SET content = CONCAT(content, ?), "
                "content_tokenized = CONCAT(COALESCE(content_tokenized,''), ' ', ?), "
                "embedded = 0 "
                "WHERE id=? AND deleted_at IS NULL",
                (marker, tokenize(text), rid))
            affected += n
        except Exception as exc:
            errors.append(f"id={rid}: {exc}")
    _piggyback()
    msg = f"OK: 已追加到 {affected}/{len(id_list)} 条"
    if errors:
        msg += "\n错误: " + "; ".join(errors[:5])
    return msg


@mcp.tool(
    description=(
        "多路检索（草稿纸 / 压缩档案）。\n\n"
        "参数:\n"
        "  query: 查询串。**多线索用空格分隔**（如 \"激光 地月 通信 时延\"）——\n"
        "         空格就是线索边界，会逐条线索检索后集成；写成长句反而更差。\n"
        "  mode:  **auto = 精确搜索**（默认；向量+全文+线索融合，空则降级LIKE）\n"
        "         **vector = 模糊搜索**（只走向量隐式关联）——**「想不起来了 / 只记得大概」\n"
        "         就用它**：此时你给的词可能记错，字面路反而拖后腿。默认返回条数更大（10），\n"
        "         且结果一律附「语义模式」提示。\n"
        "         | fulltext(只走全文路) | clue(只走线索路) | keyword(LIKE串联)\n"
        "         | condition(原样SQL条件，供数值探针)\n"
        "  scope: draftpad(默认) | compaction | all\n"
        "  bucket: 限定主题桶（省略=跨桶，默认行为）\n"
        "  tags:  标签过滤\n"
        "  limit: 条数（默认 5）\n"
        "  condition: mode=condition 时的 SQL 条件，如 \"content LIKE '%19.7%'\"\n"
        "  expand: 词表同义词环查询扩展，**默认开**（config.search.vocab_expand_query）。\n"
        "          查 `裁决` 时会并把环里的 `裁定` 一并搜（提召回）；只作用于字面两路。\n"
        "  rerank: 跨编码器精排，**默认开**（跟随 config.search.rerank_enabled）。\n"
        "          传 false 可**本次关掉**——精排逐条打分、取的是「最像」而不是\n"
        "          「最相关」，做**穷举/枚举型**查询时精确排序反而碍事，此时关掉。\n"
        "          ★ 对 mode=keyword / mode=condition / auto 的 LIKE 兜底分支**不生效**。\n\n"
        "**⚠️ 向量路是近似检索（ANN/HNSW）**：62.5 万点实测 top-1 命中率约 92%、\n"
        "top-10 约 100%。**可用于语义召回，不要用来做精确查找/判重/「找这一条」**——\n"
        "那会撞上漏检。精确匹配请用 fulltext / keyword / condition 模式。\n\n"
        "**关于俄文/希腊文/带变音拉丁文**：**非 CJK 查询走 `content` 列的 FULLTEXT\n"
        "（MariaDB 默认空格分词），不依赖 tokenized，正常工作**（实测俄文可命中）。\n"
        "`content_tokenized` 里的俄希拉丁为空，只影响「中文查询命中俄希拉丁内容」\n"
        "这类混合场景——而那类场景本来就该走**向量路**（多语言共享语义空间，实测\n"
        "中文查询直接命中俄文书的对应章节）。\n"
        "**残留问题**：希腊文/拉丁文的屈折变化（`Πλάτων` vs `Πλάτωνος`）会让精确词\n"
        "匹配失败，需要词形还原（lemmatization）——那是另一件工程，与 tokenized 无关。\n\n"
        "返回: 命中列表 + **检索口径说明**（用了哪条路、是否降级、是否重排）。"
    )
)
def draftpad_find(query: str = "", mode: str = MODE_AUTO,
                  scope: str = "draftpad", bucket: Optional[str] = None,
                  tags: Optional[list[str]] = None, limit: int = 0,
                  condition: str = "", condition_params: Optional[list] = None,
                  rerank: Optional[bool] = None,
                  expand: Optional[bool] = None) -> str:
    assert _cfg is not None and _retriever is not None
    # 未显式给 limit 时：模糊/语义模式默认多返回几条（记不清时该多看几条才谈得上挑）
    default_n = (_cfg.search.semantic_default_limit if mode == MODE_VECTOR
                 else _cfg.draftpad.find_default_limit)
    n = int(limit or default_n)
    n = max(1, min(n, _cfg.draftpad.find_max_limit))

    try:
        _qe = _query_expansion(query, expand)
        if scope == "compaction":
            return _archive_find_impl(query, mode, n, condition, condition_params,
                                      rerank, expand)
        if scope == "all":
            a = _retriever.find(query, mode=mode, table="draftpad", bucket=bucket,
                                tags=tags, limit=n, condition=condition,
                                condition_params=condition_params,
                                select_cols=_DRAFTPAD_COLS, rerank=rerank,
                                extra_terms=_qe[0], extra_note=_qe[1])
            b = _retriever.find(query, mode=mode, table="dsh_compaction",
                                limit=n, condition=condition,
                                condition_params=condition_params, deleted_col=None,
                                select_cols=_COMPACTION_COLS, rerank=rerank,
                                extra_terms=_qe[0], extra_note=_qe[1])
            out = ["### 草稿纸", _fmt_hits(a), "", "### 压缩档案",
                   _fmt_hits(b, kind="compaction")]
            _piggyback()
            return "\n".join(out)

        res = _retriever.find(query, mode=mode, table="draftpad", bucket=bucket,
                              tags=tags, limit=n, condition=condition,
                              condition_params=condition_params,
                              select_cols=_DRAFTPAD_COLS, rerank=rerank,
                                extra_terms=_qe[0], extra_note=_qe[1])
        _piggyback()
        return _fmt_hits(res)
    except SqlGuardError as exc:
        # 输入卫生失败：**显式报错**（不静默降级，也不执行）
        return f"REJECTED: {exc}"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


@mcp.tool(
    description=(
        "枚举账本里用过的所有标签（含出现次数）—— 让 tags 真正可用。\n\n"
        "参数: limit: 最多返回几个（默认 100）。"
    )
)
def draftpad_tags(limit: int = 0) -> str:
    assert _db is not None
    lim = int(limit or 100)
    rows = _db.query(
        "SELECT tags FROM draftpad WHERE deleted_at IS NULL "
        "AND tags IS NOT NULL AND tags != '[]'")
    counter: dict[str, int] = {}
    for r in rows:
        try:
            for t in json.loads(r["tags"] or "[]"):
                counter[str(t)] = counter.get(str(t), 0) + 1
        except Exception:
            continue
    if not counter:
        return "暂无标签"
    top = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:lim]
    lines = [f"标签 {len(counter)} 种，显示前 {len(top)}:"]
    for t, c in top:
        lines.append(f"  {c:4d}  {t}")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# 压缩档案（DSH 压缩收割）
# ──────────────────────────────────────────────

def _archive_find_impl(query: str, mode: str, limit: int, condition: str,
                       condition_params: Optional[list] = None,
                       rerank: Optional[bool] = None,
                       expand: Optional[bool] = None) -> str:
    assert _retriever is not None
    _qe = _query_expansion(query, expand)
    res = _retriever.find(query, mode=mode, table="dsh_compaction", limit=limit,
                          condition=condition, condition_params=condition_params,
                          deleted_col=None, select_cols=_COMPACTION_COLS,
                          rerank=rerank,
                          extra_terms=_qe[0], extra_note=_qe[1])
    return _fmt_hits(
        res,
        "提示: 用 archive_expand(record_id) 取摘要全文与遮蔽 seq 指针；"
        "再以 archive_fetch(record_id, seq) 回查原文（摘要是有损的，别只读摘要作答）",
        kind="compaction")


@mcp.tool(
    description=(
        "检索 DSH 压缩档案（上下文被压缩时产生的摘要）。\n\n"
        "用途: 找回'刚被压掉/之前聊过'的内容。\n"
        "参数: query / mode / limit / condition / rerank / expand —— 同 draftpad_find；\n"
        "      session: 只看某个会话的压缩档案。传裸 uuid 或 session-<uuid>"
        " 均可（前缀自动剥）；\"current\" 需进程环境有 $DSH_SESSION_ID"
        "——MCP 常驻进程通常没有，请先 bash `echo $DSH_SESSION_ID` 再原样传入。\n"
        "**⚠️ 向量路是近似检索（ANN/HNSW）**：62.5 万点实测 top-1 命中率约 92%、\n"
        "top-10 约 100%。**可用于语义召回，不要用来做精确查找/判重/「找这一条」**——\n"
        "那会撞上漏检。精确匹配请用 fulltext / keyword / condition 模式。\n\n"
        "**关于俄文/希腊文/带变音拉丁文**：**非 CJK 查询走 `content` 列的 FULLTEXT\n"
        "（MariaDB 默认空格分词），不依赖 tokenized，正常工作**（实测俄文可命中）。\n"
        "`content_tokenized` 里的俄希拉丁为空，只影响「中文查询命中俄希拉丁内容」\n"
        "这类混合场景——而那类场景本来就该走**向量路**（多语言共享语义空间，实测\n"
        "中文查询直接命中俄文书的对应章节）。\n"
        "**残留问题**：希腊文/拉丁文的屈折变化（`Πλάτων` vs `Πλάτωνος`）会让精确词\n"
        "匹配失败，需要词形还原（lemmatization）——那是另一件工程，与 tokenized 无关。\n\n"
        "**注意**: 摘要是**有损**的（压缩比约 35:1）；命中后用 archive_expand 取指针回查原文。"
    )
)
def archive_find(query: str = "", mode: str = MODE_AUTO, limit: int = 0,
                 condition: str = "", condition_params: Optional[list] = None,
                 session: str = "", rerank: Optional[bool] = None,
                 expand: Optional[bool] = None) -> str:
    assert _cfg is not None
    default_n = (_cfg.search.semantic_default_limit if mode == MODE_VECTOR
                 else _cfg.draftpad.find_default_limit)
    n = int(limit or default_n)
    n = max(1, min(n, _cfg.draftpad.find_max_limit))
    if session:
        raw = session.strip()
        if raw == "current":
            # 实测（2026-09-22）：MCP server 是**跨会话常驻进程**，其环境里
            # 没有 per-session 的 $DSH_SESSION_ID ⇒ 这里必然取不到。
            # 故引导调用方自己用 bash 取，再把结果原样传进来。
            raw = os.environ.get("DSH_SESSION_ID", "").strip()
            if not raw:
                return ("ERROR: 本进程环境无 $DSH_SESSION_ID"
                        "（MCP server 常驻，拿不到 per-session 变量）。"
                        "请先 bash `echo $DSH_SESSION_ID`，再把结果**原样**传给"
                        " session=（session- 前缀会自动剥掉）。")
        # 库里 dsh_session 存的是**裸 uuid**；而环境变量与日志目录名都带
        # session- 前缀 ⇒ 这里统一剥掉，让调用方「拿到什么就传什么」。
        sid = raw[len("session-"):] if raw.startswith("session-") else raw
        # 与既有 condition 合并（archive_find 的 session 只是 condition 的糖）
        cond = "dsh_session = ?"
        cparams = [sid]
        if condition.strip():
            cond = f"({condition}) AND ({cond})"
            cparams = list(condition_params or []) + cparams
        condition, condition_params = cond, cparams
    try:
        return _archive_find_impl(query, mode, n, condition, condition_params,
                                  rerank, expand)
    except SqlGuardError as exc:
        return f"REJECTED: {exc}"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


# ──────────────────────────────────────────────
# 跨源统一检索（search_OMNIA）
# ──────────────────────────────────────────────

@mcp.tool(
    description=(
        "跨源统一检索：一次调用同时搜【草稿纸 + 会话正文 + 压缩摘要】，"
        "每个源按其能力走 向量 + FULLTEXT + LIKE，归一化后融合排序。\n\n"
        "用途: 找『以前在哪儿写过/聊过 X』，而不确定它落在草稿纸、会话记录、\n"
        "      还是已被压缩掉的上文里。写长篇小说/设定集时尤其常用。\n"
        "**不替代** draftpad_find / archive_find —— 精细模式（vector / fulltext /\n"
        "clue / keyword / condition / rerank / expand）仍然在那两个工具里，\n"
        "本工具只负责『先找到大概在哪』。\n\n"
        "参数:\n"
        "  query:            查询串。CJK 走 unigram+bigram（单字/任意子串可搜）。\n"
        "  limit:            **每一路各取多少条**（默认 5，上限 50）。\n"
        "                    ★ 这不是最终返回条数：三路各取 limit 条 → 去重 →\n"
        "                      剩多少给多少，不截断回 limit（确保不漏）。\n"
        "                      所以传 5 常常会拿回 15–25 条，这是有意的。\n"
        "  sources:          限定源，逗号分隔：draftpad / messages / compactions；缺省全搜。\n"
        "  modes:            限定路，逗号分隔：vector / fulltext / like；缺省三路全开。\n"
        "                    关掉 like 能省时间——会话正文的 LIKE 是 14 万行全表扫（约 1–3 秒）。\n"
        "  per_source_limit: 覆盖每路取数（一般不用传，默认跟随 limit）。\n\n"
        "能力边界（必须知道，否则会误判『库里没有』）:\n"
        "  · 会话正文（dsh_message）**没有向量列**，该源只走全文+LIKE；\n"
        "    语义（模糊）召回由草稿纸与压缩摘要承担。\n"
        "  · 压缩摘要是**有损**的（约 35:1）；命中后必须 archive_expand → archive_fetch\n"
        "    回查原文，不要只读摘要作答。\n"
        "  · **俄文 / 希腊文 / 带变音拉丁文**：非 CJK 查询走 `content` 列的 FULLTEXT\n"
        "    （默认空格分词）**不依赖 tokenized，正常工作**；`content_tokenized` 里\n"
        "    这些文字为空，只影响「中文查询命中它们」的混合场景——那该走向量路。\n"
        "    残留问题：希腊/拉丁文的屈折变化（`Πλάτων` vs `Πλάτωνος`）使精确词匹配\n"
        "    失败，需词形还原，属另一件工程。\n"
        "  · **向量路是 HNSW 近似检索**（62.5 万点实测 top-1 命中率 ~92%、top-10 ~100%）。\n"
        "    适合语义召回；**不要用它判重/找「就是这一条」**，那会漏。精确查找走 fulltext/like。\n"
        "  · 会话正文命中后，用 message_context(guid, before, after) 取该行前后的上下文。\n\n"
        "返回: 统一命中列表（源 / 分数 / 命中路 / 片段）+ 检索口径 + 回溯指引。"
    )
)
def search_OMNIA(query: str = "", limit: int = 5, sources: str = "",
                 modes: str = "", per_source_limit: int = 0) -> str:
    assert _retriever is not None
    srcs = [s.strip() for s in (sources or "").split(",") if s.strip()]
    mds = [m.strip() for m in (modes or "").split(",") if m.strip()]
    n = max(1, min(int(limit or 5), 50))
    ps = max(0, min(int(per_source_limit or 0), 200))
    try:
        searcher = OmnibusSearcher(_retriever)
        res = searcher.search(
            query, limit=n,
            sources=srcs or list(OMNIA_DEFAULT_SOURCES),
            modes=mds or list(OMNIA_DEFAULT_MODES),
            per_source_limit=ps,
        )
        return omnia_format(res)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {type(exc).__name__}: {exc}"


@mcp.tool(
    description=(
        "按 guid 取一条会话正文，并展开它前后的上下文（同一会话、同一代日志）。\n\n"
        "用途: search_OMNIA 或 draftpad_find 命中「会话正文」后，\n"
        "      看那一段到底前后说了什么——否则只有片段容易误读。\n\n"
        "参数:\n"
        "  guid:     dsh_message.guid（search_OMNIA 的命中里带；64 位十六进制）。\n"
        "  before:   往前取多少条（默认 3，上限 30）。\n"
        "  after:    往后取多少条（默认 3，上限 30）。\n"
        "  max_chars: 每条正文截断字符数（默认 1200，上限 8000）。\n\n"
        "说明:\n"
        "  · 按 line_no（解压后物理行号）取相邻，同会话同 source_gens 内。\n"
        "  · 目标行以 ▶ 标记；正文里的换行压成空格以便阅读。\n"
        "  · 这是**只读快照**：DSH 仍在追加的会话，最新几条可能还没抄进来\n"
        "    （抄录器 60 秒一轮，本场会话通常延迟 <1 分钟）。"
    )
)
def _message_context_text(db, guid: str = "", before: int = 3, after: int = 3,
                          max_chars: int = 1200) -> str:
    """按 guid 取一条会话正文并展开前后 N 条。**纯逻辑，db 由调用方给**。

    为什么带 db 参数而不是读模块全局 `_db`：
      HTTP 桥跑在独立线程、用独立连接（mariadb 连接对象不是线程安全的）。
      如果这里读 `_db`，桥要么污染 MCP 主线程的全局，要么跟主线程抢同一个连接。
      传参之后两边各用各的连接，零竞态。
    """
    guid = (guid or "").strip()
    if not guid:
        return "需要 guid（search_OMNIA 的「会话正文」命中里带）"
    try:
        head = db.query_one(
            "SELECT session_id, source_gens, line_no, ev_type, kind, role, ev_time "
            "FROM dsh_message WHERE guid = ?",
            (guid,),
            readonly=True,
        )
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {type(exc).__name__}: {exc}"
    if not head:
        return f"未找到 guid={guid} 的行（可能尚未抄录，或 guid 抄错）"

    b = max(0, min(int(before or 0), 30))
    a = max(0, min(int(after or 0), 30))
    cap = max(120, min(int(max_chars or 1200), 8000))
    target = int(head["line_no"])
    sid, gens = head["session_id"], head["source_gens"]
    cols = "guid, line_no, ev_type, kind, role, ev_time, body, ordinal"

    # ★ 不能按「line_no ± N」取窗口：line_no 是**事件物理行号**，而抄录器跳过
    #   chunk 家族（assistant/chunk 等）⇒ 行号是稀疏的，±N 常常取不到任何邻行
    #   （实测 before=2/after=2 只返回 1 条）。正解是按 line_no 排序取**相邻 N 条**：
    #   向前取「line_no <= 目标」的最后 b 条，向后取「line_no > 目标」的最前 a 条。
    try:
        back = db.query(
            f"SELECT {cols} FROM dsh_message "
            "WHERE session_id = ? AND source_gens = ? AND line_no <= ? "
            "ORDER BY line_no DESC, ordinal DESC LIMIT ?",
            (sid, gens, target, b + 1), readonly=True,
        )
        fwd = db.query(
            f"SELECT {cols} FROM dsh_message "
            "WHERE session_id = ? AND source_gens = ? AND line_no > ? "
            "ORDER BY line_no ASC, ordinal ASC LIMIT ?",
            (sid, gens, target, a), readonly=True,
        )
        rows = sorted(back + fwd, key=lambda r: (r["line_no"], r["ordinal"]))
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {type(exc).__name__}: {exc}"

    lines = [
        f"会话 {head['session_id']} · 目标 L{target} · "
        f"取回 {len(rows)} 条（前 {len(back) - 1} / 后 {len(fwd)}）",
        "",
    ]
    for r in rows:
        mark = "▶" if str(r["guid"]) == guid else " "
        body = " ".join(str(r.get("body") or "").split())
        if len(body) > cap:
            body = body[:cap] + f"…（截断，原长 {len(body)} 字）"
        lines.append(
            f"{mark} L{r['line_no']} [{r['ev_type']}/{r['kind']}]"
            f"{(' role=' + r['role']) if r.get('role') else ''}"
            f" · {r['ev_time']}"
        )
        lines.append(f"   {body or '(空正文)'}")
    return "\n".join(lines)


@mcp.tool(
    description=(
        "按 guid 取一条会话正文，并展开它前后的上下文（同一会话、同一代日志）。\n\n"
        "用途: search_OMNIA 或 draftpad_find 命中「会话正文」后，\n"
        "      看那一段到底前后说了什么——否则只有片段容易误读。\n\n"
        "参数:\n"
        "  guid:     dsh_message.guid（search_OMNIA 的命中里带；64 位十六进制）。\n"
        "  before:   往前取多少条（默认 3，上限 30）。\n"
        "  after:    往后取多少条（默认 3，上限 30）。\n"
        "  max_chars: 每条正文截断字符数（默认 1200，上限 8000）。\n\n"
        "说明:\n"
        "  · 按 line_no（解压后物理行号）取相邻，同会话同 source_gens 内。\n"
        "  · 目标行以 ▶ 标记；正文里的换行压成空格以便阅读。\n"
        "  · 这是**只读快照**：DSH 仍在追加的会话，最新几条可能还没抄进来\n"
        "    （抄录器 60 秒一轮，本场会话通常延迟 <1 分钟）。"
    )
)
def message_context(guid: str = "", before: int = 3, after: int = 3,
                    max_chars: int = 1200) -> str:
    """MCP 工具壳：把 MCP 自己的连接交给 `_message_context_text`。

    HTTP 桥走同一个实现，但传它自己的连接（见 omnibus_http.do_context）——
    两边各用各的连接，零竞态。
    """
    assert _db is not None
    return _message_context_text(_db, guid, before, after, max_chars)


@mcp.tool(
    description=(
        "展开一条压缩档案：返回摘要全文 + 遮蔽 seq 指针 + 防漏网层（可选）。\n\n"
        "参数: record_id(必填); with_excerpts(true=附带 L3 防漏网层，较长)\n"
        "返回: 摘要（标注有损）+ 被遮蔽的事件 seq 列表（可据此在原会话日志里回查原文）。"
    )
)
def archive_expand(record_id: int, with_excerpts: bool = False) -> str:
    assert _db is not None
    row = _db.query_one(
        "SELECT id, dsh_session, compaction_id, turn_no, occurred_at, "
        "shadowed_count, shadowed_tokens, shadowed_range_start, shadowed_range_end, "
        "shadowed_seqs, summary, summary_chars, summary_excerpts, provider, model, "
        "input_tokens, output_tokens, cache_read_tokens "
        "FROM dsh_compaction WHERE id=?", (int(record_id),))
    if row is None:
        return f"NOT_FOUND: 压缩档案 id={record_id} 不存在"

    seqs = []
    try:
        seqs = json.loads(row.get("shadowed_seqs") or "[]")
    except Exception:
        pass

    out = [
        f"# 压缩档案 id={row['id']}",
        f"- 会话: {row['dsh_session']}  turn: {row['turn_no']}",
        f"- 时间: {row['occurred_at']}",
        f"- 遮蔽: {row['shadowed_count']} 条事件 / {row['shadowed_tokens']} tokens"
        f"  (位置跨度 {row['shadowed_range_start']}..{row['shadowed_range_end']})",
        f"- 摘要模型: {row['provider']}/{row['model']}"
        f"  (in={row['input_tokens']} out={row['output_tokens']} cacheRead={row['cache_read_tokens']})",
        "",
        "## ⚠️ 摘要（**有损**，可能失真；回查原文请用下面的 seq）",
        str(row.get("summary") or "")[:20000],
    ]
    if seqs:
        out.append("")
        out.append(f"## 遮蔽 seq 指针（{len(seqs)} 个，前 60 个）")
        out.append(", ".join(str(s) for s in seqs[:60]) + (" …" if len(seqs) > 60 else ""))
        out.append(f"\n回查方式: archive_fetch(record_id={row['id']}, seq=<上面的某个 seq>) 取回原文。")
    if with_excerpts:
        out.append("")
        if row.get("summary_excerpts"):
            out.append("## L3 防漏网层（被遮蔽事件首段）")
            out.append(str(row["summary_excerpts"])[:30000])
        else:
            # 不静默：excerpts 只在「被遮蔽事件恰好落在本次收割批次」时才抽得到，
            # 更早批次的事件在 seq_index 里不可见（由 L2 的 seq 指针承担回查职责）。
            out.append("## L3 防漏网层：本条未抽到"
                       "（被遮蔽事件不在收割批次内，seq_index 不可见）"
                       " —— 回查原文请用 archive_grep / archive_fetch 按 seq 取")
    return "\n".join(out)


@mcp.tool(
    description=(
        "取回被遮蔽事件的原文 —— 压缩档案闭环的最后一步。**seq 与 guid 两种入口都行**。\n\n"
        "闭环: archive_find(找压缩点) → archive_expand(拿指针) → **archive_fetch(取原文)**\n"
        "摘要是有损的（约 35:1），所以定位到压缩点后**必须**回到原文核对。\n\n"
        "参数:\n"
        "  record_id: 压缩档案 id。\n"
        "  seq:       该档案遮蔽集内的 seq（可省略，改用 guid）。\n"
        "  guid:      该档案遮蔽集内的 guid（可省略，改用 seq）。两者给一个即可。\n"
        "  max_chars: 返回字符上限（默认 20000）。\n\n"
        "两条取数路线（自动择优）:\n"
        "  · v2 路线（优先）: 用 guid 从 **dsh_message** 取 —— 我们自己抄下来的正文，\n"
        "    **不依赖源日志**，也不受 v3→v4 重编号 seq 的影响。\n"
        "    传 seq 时会在该档案的代际内自动译成 guid。\n"
        "  · v1 路线（回退）: 旧档案用 seq 指针 + 读**源日志文件**。源日志一旦消失\n"
        "    （实测已有 17 份消失且无备份）此路即断，故仅作回退。\n\n"
        "安全: 只允许取**该档案遮蔽集内**的指针，越界直接拒绝。\n"
        "注意: 单条 assistant/message 可能含**多个 ContentBlock**（实测一条 11954+5135 字），\n"
        "      默认窗口已按此调大；若仍被截断会显式提示（不静默）。"
    )
)
def archive_fetch(record_id: int, seq: int = 0, guid: str = "",
                  max_chars: int = 20000) -> str:
    assert _db is not None

    row = _db.query_one(
        "SELECT id, compaction_id, file_path, dsh_session, shadowed_seqs "
        "FROM dsh_compaction WHERE id=?", (int(record_id),))
    if row is None:
        return f"NOT_FOUND: 压缩档案 id={record_id} 不存在"

    # ── 路线 A（优先）：v2 的 guid 指针 —— 从 dsh_message 取，不碰源日志 ──
    #    为什么优先：v1 的两条依赖都会断 —— seq 会因官方 v3→v4 迁移重编号，
    #    源日志会消失（实测 17 份已没且无备份）。v2 的 guid 与 seq 脱钩，
    #    正文又是我们自己抄的，自足。
    v2 = _db.query_one(
        "SELECT session_id, source_gens, shadowed_guids FROM dsh_compaction_v2 "
        "WHERE compaction_id=?", (row.get("compaction_id") or "",))
    guids_json = (v2 or {}).get("shadowed_guids") or ""
    if v2 and guids_json.strip() not in ("", "[]"):
        try:
            allowed_g = set(json.loads(guids_json))
        except Exception:
            allowed_g = set()
        target = (guid or "").strip()
        if not target and seq:
            # seq → guid：严格限定在该档案自己的代际内（跨代际 seq 会撞车）
            hit = _db.query_one(
                "SELECT guid FROM dsh_message "
                "WHERE session_id=? AND source_gens=? AND seq=? LIMIT 1",
                (v2["session_id"], v2.get("source_gens"), int(seq)))
            target = (hit or {}).get("guid") or ""
            if not target:
                return (f"NOT_FOUND: seq={seq} 在档案 id={record_id} 的代际"
                        f"(gens={v2.get('source_gens')}) 内没有对应 guid"
                        f" —— 该 seq 或属别的代际，或本就没有正文块。"
                        f"可改用 guid 入参，或 archive_expand 看合法指针")
        if not target:
            return (f"REJECTED: 请给 seq 或 guid 之一"
                    f"（本档案 v2 遮蔽 {len(allowed_g)} 条）")
        if target not in allowed_g:
            return (f"REJECTED: guid={target[:16]}… 不在档案 id={record_id} 的遮蔽集内"
                    f"（该档案遮蔽 {len(allowed_g)} 条）—— 请用 archive_expand 查合法指针")

        msg = _db.query_one(
            "SELECT guid, session_id, source_gens, seq, ev_type, body "
            "FROM dsh_message WHERE guid=?", (target,))
        if msg is None:
            return (f"NOT_FOUND: guid={target[:16]}… 在 dsh_message 里不存在"
                    f"（v2 指针指向了一条没有正文的行）")
        body = msg.get("body") or "（该事件无正文）"
        head = (f"# guid={target[:16]}…  seq={msg.get('seq')}  type={msg.get('ev_type')}\n"
                f"# 会话={msg.get('session_id')}  档案id={record_id}"
                f"  gens={msg.get('source_gens')}"
                f"  （v2 guid 指针 / 取自 dsh_message，未依赖源日志）\n\n")
        tail = ""
        if len(body) > max_chars:
            tail = (f"\n\n…（已截断：原文共 {len(body)} 字符，此处显示前 {max_chars}；"
                    f"调大 max_chars 取更多）")
        return head + body[:max_chars] + tail

    # ── 路线 B（回退）：v1 的 seq 指针 + 读源日志 ──
    from melquiades.harvest.extract import event_full_text
    from melquiades.harvest.reader import read_new_lines, split_lines_to_events

    if not seq:
        return (f"REJECTED: 档案 id={record_id} 尚未建立 v2 guid 指针"
                f"（shadowed_guids 为空），只能按 seq 取；请传 seq")

    try:
        allowed = set(json.loads(row.get("shadowed_seqs") or "[]"))
    except Exception:
        allowed = set()
    if int(seq) not in allowed:
        return (f"REJECTED: seq={seq} 不在档案 id={record_id} 的遮蔽集内"
                f"（该档案遮蔽 {len(allowed)} 条）—— 请用 archive_expand 查合法 seq")

    fp = row.get("file_path") or ""
    if not fp or not os.path.exists(fp):
        return (f"NOT_FOUND: 会话日志不存在或已被移除: {fp}"
                f"（v1 路线依赖源日志；本档案 v2 指针为空，故无从回退）")

    res = read_new_lines(fp, 0)
    if res.error:
        return f"ERROR: 读会话日志失败: {res.error}"
    events = split_lines_to_events(res.lines)
    ev = next((e for e in events if e.get("seq") == int(seq)), None)
    if ev is None:
        return f"NOT_FOUND: seq={seq} 在该日志中不存在（日志共 {len(events)} 条事件）"

    body = event_full_text(ev) or "（该事件类型无文本内容）"
    head = (f"# seq={seq}  type={ev.get('type')}\n"
            f"# 会话={row.get('dsh_session')}  档案id={record_id}"
            f"  （v1 seq 指针 / 源日志 {len(events)} 条事件 / 本档案遮蔽 {len(allowed)} 条）\n\n")
    tail = ""
    if len(body) > max_chars:
        # 截断必须可见（不静默）—— 否则会像"只看到第一个 ContentBlock"那样误判原文缺失
        tail = (f"\n\n…（已截断：原文共 {len(body)} 字符，此处显示前 {max_chars}；"
                f"调大 max_chars 取更多）")
    return head + body[:max_chars] + tail


@mcp.tool(
    description=(
        "在**某个压缩点的遮蔽区内**按关键词搜原文 —— 补上「定位到具体 seq」这一环。\n\n"
        "为什么需要它: archive_find 只搜摘要+L3（查库，毫秒级），"
        "而摘要是有损的；若关键词只在原文里，就定位不到那 719 条中的哪一条。\n"
        "本工具只读**该档案对应的那一个会话日志**（约 0.5s），比全量扫描全部会话精准得多。\n\n"
        "参数:\n"
        "  record_id: 压缩档案 id。\n"
        "  query:     关键词（jieba 分词，**全部词出现**才算命中）。\n"
        "  limit:     最多返回几条（默认 5）。\n\n"
        "返回: 命中的 seq + 原文片段 —— 再用 archive_fetch(record_id, seq) 取全文。"
    )
)
def archive_grep(record_id: int, query: str, limit: int = 5) -> str:
    assert _db is not None
    from melquiades.harvest.extract import event_full_text
    from melquiades.harvest.reader import read_new_lines, split_lines_to_events
    from melquiades.tokenizer import split_keywords

    row = _db.query_one(
        "SELECT id, file_path, dsh_session, shadowed_seqs "
        "FROM dsh_compaction WHERE id=?", (int(record_id),))
    if row is None:
        return f"NOT_FOUND: 压缩档案 id={record_id} 不存在"

    kws = split_keywords(query)
    if not kws:
        return "ERROR: query 不能为空"

    try:
        allowed = set(json.loads(row.get("shadowed_seqs") or "[]"))
    except Exception:
        allowed = set()
    if not allowed:
        return f"NOT_FOUND: 档案 id={record_id} 没有遮蔽 seq 指针"

    fp = row.get("file_path") or ""
    if not fp or not os.path.exists(fp):
        return f"NOT_FOUND: 会话日志不存在或已被移除: {fp}"

    res = read_new_lines(fp, 0)
    if res.error:
        return f"ERROR: 读会话日志失败: {res.error}"
    events = split_lines_to_events(res.lines)

    hits: list[tuple[int, str, str]] = []
    for ev in events:
        seq = ev.get("seq")
        if seq not in allowed:
            continue
        body = event_full_text(ev)
        if not body:
            continue
        low = body.lower()
        if all(k.lower() in low for k in kws):
            hits.append((seq, ev.get("type", ""), body))
            if len(hits) >= int(limit):
                break

    if not hits:
        return (f"未命中（档案 {record_id} 遮蔽 {len(allowed)} 条中，"
                f"没有同时含 {kws} 的事件）")

    out = [f"命中 {len(hits)} 条（档案 id={record_id}，遮蔽集 {len(allowed)} 条，"
           f"关键词 {kws}）"]
    for seq, typ, body in hits:
        out.append(f"\n[seq={seq} {typ}]")
        out.append(f"  {body[:300]}")
    out.append(f"\n取全文: archive_fetch(record_id={record_id}, seq=<上面的 seq>)")
    return "\n".join(out)


@mcp.tool(
    description=(
        "压缩档案与收割器状态：游标数、档案条数、最近收割运行记录。\n\n"
        "用于确认'收割是否在正常工作'（不静默降级）。"
    )
)
def archive_status(logs: int = 5) -> str:
    assert _cfg is not None and _db is not None and _store is not None
    total = _db.scalar("SELECT COUNT(*) FROM dsh_compaction") or 0
    sessions = _db.scalar("SELECT COUNT(DISTINCT dsh_session) FROM dsh_compaction") or 0
    sh_tok = _db.scalar("SELECT COALESCE(SUM(shadowed_tokens),0) FROM dsh_compaction") or 0
    cursors = _db.scalar("SELECT COUNT(*) FROM dsh_harvest_cursor") or 0

    lines = [
        f"压缩档案: {total} 条 / {sessions} 个会话 / 遮蔽合计 {sh_tok} tokens",
        f"收割游标: {cursors} 个文件",
        f"定时器  : {'运行中' if (_scheduler and _scheduler.running) else '未运行'}"
        f" (间隔 {_cfg.dsh.scan_interval_sec}s)",
        "",
        f"最近 {int(logs)} 次收割:",
    ]
    for r in _store.recent_logs(limit=int(logs)):
        lines.append(
            f"  [{r['status']:8s}] {str(r['started_at'])[:19]} {r['trigger_kind']:8s} "
            f"files={r['files_scanned']}/{r['files_seen']} frames={r['frames_read']} "
            f"comp={r['compactions_new']} adv={r['cursor_advanced']}"
            + (f" | {str(r['error_text'])[:80]}" if r['error_text'] else ""))
    return "\n".join(lines)


# ──────────────────────────────────────────────
# 启动
# ──────────────────────────────────────────────

def _init_state(config_path: str) -> None:
    global _cfg, _db, _retriever, _store, _runner, _scheduler
    _cfg = Config.load(config_path)
    _db = MariaDB(_cfg.mariadb)
    ok, msg = _db.ping()
    if not ok:
        raise RuntimeError(f"MariaDB 不可用: {msg}")
    logger.info("MariaDB connected: %s", msg)

    # 权限自检：本应用不需要 DROP/FILE/SUPER 等；有则告警（不静默）
    try:
        priv = _db.check_privileges()
        if priv.get("risky"):
            logger.warning(
                "⚠️ 数据库用户持有本应用不需要的权限: %s —— 建议撤销"
                "（REVOKE DROP ON `%s`.* FROM ...）",
                ", ".join(priv["risky"]), _cfg.mariadb.database,
            )
        else:
            logger.info("privilege check: 无多余危险权限 ✓")
    except Exception as exc:
        logger.warning("privilege check skipped: %s", exc)

    _retriever = Retriever(_cfg, _db)
    _store = HarvestStore(_db)
    _store.ensure_tables()
    vec_status = ensure_all_vectors(_db)
    logger.info("vector columns: %s", vec_status)

    _runner = HarvestRunner(_cfg, _store, _retriever)

    # 清理孤儿运行记录（进程被 kill 时 log_finish 不会执行）
    try:
        n = _store.cleanup_stale_runs(minutes=_cfg.dsh.stale_run_minutes)
        if n:
            logger.info("cleaned %d stale harvest run(s)", n)
    except Exception as exc:
        logger.warning("cleanup_stale_runs failed: %s", exc)


def _start_triggers() -> None:
    """启动三触发中的两个常驻项（第三个是工具搭车，见 _piggyback）。"""
    global _scheduler
    assert _cfg is not None and _runner is not None

    # C. 启动扫（增量，后台线程，不阻塞 MCP initialize）
    if _cfg.dsh.startup_scan_enabled:
        spawn_fire_and_forget(_runner.startup_scan)

    # A. 定时器
    if _cfg.dsh.harvest_enabled:
        _scheduler = HarvestScheduler(
            _runner,
            interval_sec=_cfg.dsh.scan_interval_sec,
            stop_wait_timeout_sec=_cfg.dsh.stop_wait_timeout_sec,
        )
        _scheduler.start()


# ── HTTP 桥（与 MCP 同生共死）─────────────────────────────────────────
#
# 为什么是进程内线程而不是独立进程：
#   · 同生共死不需要额外的看护者 —— MCP 起来桥就在，MCP 死桥就没了，
#     不会留孤儿进程占着 9010 端口
#   · 省掉一层「谁先起、起没起来、起来了几份」的状态同步
#
# 为什么端口被占不致命：MCP 是本命，桥只是 DSH 侧搜索 tab 的后端。
# 9010 被占（比如手动跑了一份 standalone）时，告警一行继续跑，
# 其余所有 MCP 工具照常工作。

def _start_http_bridge() -> None:
    """在 MCP 进程内起检索桥。任何失败都只告警，绝不让 MCP 起不来。"""
    global _bridge
    assert _cfg is not None

    if not _cfg.bridge.enabled:
        logger.info("http bridge: 已禁用（config [bridge] enabled=false）")
        return

    try:
        from .omnibus_http import start_in_thread

        _bridge = start_in_thread(_cfg, host=_cfg.bridge.host, port=_cfg.bridge.port)
    except OSError as exc:
        logger.warning(
            "http bridge 启动失败（%s）—— MCP 继续运行，但 DSH 的「统一检索」tab 会不可用。"
            "通常是 %s:%d 已被占用（手动跑了一份 melquiades-http？）",
            exc,
            _cfg.bridge.host,
            _cfg.bridge.port,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("http bridge 启动异常（%s: %s）—— MCP 继续运行", type(exc).__name__, exc)


def _stop_http_bridge() -> None:
    global _bridge
    if _bridge is None:
        return
    try:
        from .omnibus_http import stop_bridge

        stop_bridge(_bridge)
    except Exception as exc:  # noqa: BLE001
        logger.warning("http bridge 停止出错（忽略）: %s", exc)
    finally:
        _bridge = None


def main() -> None:
    """启动 melquiades 服务（stdio）。"""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # 第三方库降噪（httpx 每个请求一行 INFO，回填时会刷屏）
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    config_path = sys.argv[1] if len(sys.argv) > 1 else str(default_config_path())
    logger.info("loading config: %s", config_path)

    _init_state(config_path)
    _start_triggers()
    _start_http_bridge()

    try:
        mcp.run()
    finally:
        _stop_http_bridge()
        if _scheduler is not None:
            _scheduler.stop()


if __name__ == "__main__":
    main()
