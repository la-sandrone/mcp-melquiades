"""
文件名: melquiades/sqlguard.py
摘要: SQL 输入卫生 —— 标识符白名单 + SQL 片段门禁 + 值转义。

设计立场（2026-09-22，与用户对齐）:
  · **真正的注入防线是绑定参数（? 占位符）**，本模块只做「早失败 + 输入卫生」。
  · **不禁止 `%` / `_` / `'` / `;` 之类的字符** —— 真实学术文本里遍地都是
    （实测 45 个失败 SQL 中 43 个含这类符号：`50%`、`Ω_Λ`、`O'Neill`、`Domènech`）。
    禁掉等于废掉功能。正确做法是**转义 + 参数化**：
      - 值 → `?` 绑定参数（tokenizer.escape_like 负责 LIKE 通配符转义）
      - 标识符（表名/列名）→ 无法参数化 ⇒ **白名单**
      - SQL 片段（condition）→ 无法参数化 ⇒ **结构门禁**（禁多语句/DML/DDL/注释 + 长度）
  · condition 推荐写法：结构里用 `?`，值走 `condition_params` —— 这样连片段也是参数化的。

依赖: 无（纯函数，便于单测）
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

try:
    import sqlglot
    from sqlglot import exp
except ImportError:  # pragma: no cover
    sqlglot = None  # type: ignore[assignment]
    exp = None  # type: ignore[assignment]

# ── AST 白名单（sqlglot 校验用）──
# 只放行「列引用 / 字面量 / 比较 / 逻辑 / 白名单函数」——实测合法 condition 只用到这些。
# 任何不在表内的节点类型 ⇒ 拒绝（白名单天然比黑名单安全：新语法默认被拒）。
_ALLOWED_AST_NODES: frozenset[str] = frozenset({
    # 引用与字面量
    "Column", "Identifier", "Literal", "Boolean", "Null", "Star",
    # 绑定参数占位符（condition_params 的核心用法：`content LIKE ?`）
    "Placeholder",
    # 分组与逻辑
    "Paren", "And", "Or", "Not", "Xor",
    # 比较
    "EQ", "NEQ", "GT", "GTE", "LT", "LTE", "Is", "Between", "In",
    # 字符串/模式匹配
    "Like", "ILike", "RegexpLike", "RegexpILike", "Escape",
    # 算术
    "Add", "Sub", "Mul", "Div", "Mod", "Neg", "IntDiv",
    # 白名单函数
    "Length", "Lower", "Upper", "Trim", "Coalesce", "Concat",
    "Substring", "Replace", "Round", "Abs", "Cast",
    # 元/杂项（sqlglot 包装节点）
    "Expression", "Condition", "Predicate", "Binary", "Unary",
})

# ── 标识符白名单 ──

ALLOWED_TABLES: frozenset[str] = frozenset({"draftpad", "dsh_compaction"})

# ── SELECT 列清单：**唯一真相源** ──
# retrieval.py / server.py 一律 import 下面的常量，禁止各自手写副本：
# guard_select_cols 是**整串**白名单匹配，任何一处字符差异都会被拒绝。
#: Hit.content 的截断宽度。**唯一真相源**：列的 SQL 与需要"展示宽度"的下游
#: （retrieval._rerank_hits 判断要不要额外取正文）都引用它，禁止再写 400 字面量。
DISPLAY_TEXT_CHARS = 400

COLS_DRAFTPAD = (
    "id, session_id, note_key, "
    f"LEFT(content, {DISPLAY_TEXT_CHARS}) AS content, tags, created_at"
)
# dsh_compaction 额外带出 turn_no / shadowed_count / shadowed_tokens ——
# 它们是判断「这条压缩档案值不值得展开」的依据（只看截断后的摘要看不出量级）
COLS_COMPACTION = (
    "id, dsh_session AS session_id, '' AS note_key, "
    f"LEFT(summary, {DISPLAY_TEXT_CHARS}) AS content, "
    "'[]' AS tags, occurred_at AS created_at, "
    "turn_no, shadowed_count, shadowed_tokens"
)
COLS_FULL = "id, session_id, note_key, content, tags, created_at"

ALLOWED_SELECT_COLS: frozenset[str] = frozenset({
    COLS_DRAFTPAD,
    COLS_COMPACTION,
    COLS_FULL,
})

# ── condition 片段门禁 ──

# 结构性禁用序列（多语句 / 注释 / 空字节）
_FORBIDDEN_SEQUENCES: tuple[tuple[str, str], ...] = (
    (";", "分号（多语句）"),
    ("--", "行注释"),
    ("/*", "块注释"),
    ("*/", "块注释"),
    ("#", "井号注释"),
    ("\x00", "空字节"),
)

# 危险关键字（词边界、大小写不敏感）—— 破坏性或读取系统信息的语句
_DANGEROUS_KEYWORDS: frozenset[str] = frozenset({
    # DML / DDL / DCL
    "insert", "update", "delete", "drop", "truncate", "alter", "create", "rename",
    "replace", "grant", "revoke",
    # 文件 / 系统
    "outfile", "dumpfile", "load_file", "load", "into", "shutdown", "kill",
    "execute", "prepare", "handler", "call",
    # 元数据 / 其它库
    "information_schema", "performance_schema", "mysql", "sys",
    # 子查询 / 联合（condition 里不该出现）
    "select", "from", "union",
    # 延时/探测
    "sleep", "benchmark",
})

# 允许出现在 condition 里的关键字（供排错提示用；不参与校验）
_ALLOWED_HINTS: frozenset[str] = frozenset({
    "and", "or", "not", "like", "in", "between", "is", "null", "escape",
    "regexp", "rlike", "true", "false",
})

_WORD_RE = re.compile(r"[a-z_][a-z0-9_]*")
_DEFAULT_MAX_LEN = 2000


class SqlGuardError(ValueError):
    """输入未通过 SQL 安全门禁。"""


def guard_table(table: str) -> str:
    """表名白名单校验（无法参数化的标识符只能白名单）。"""
    if not isinstance(table, str) or table not in ALLOWED_TABLES:
        raise SqlGuardError(
            f"表名不在白名单内: {table!r}；允许: {sorted(ALLOWED_TABLES)}"
        )
    return table


def guard_select_cols(cols: str) -> str:
    """SELECT 列清单白名单校验（列清单是结构，不能参数化）。"""
    if not isinstance(cols, str) or cols not in ALLOWED_SELECT_COLS:
        raise SqlGuardError("select_cols 不在白名单内（只允许预定义列清单）")
    return cols


def guard_condition(cond: str, max_len: int = _DEFAULT_MAX_LEN) -> str:
    """
    校验 SQL 片段（condition）。

    ★ 改用 **sqlglot 解析 + AST 白名单**（2026-09-22）——
      理由：手写关键字黑名单是最脆弱的防线（漏一个就完蛋），
      而白名单 + 真解析器是「只放行我能理解的语法」。

    校验链（任一失败即抛 SqlGuardError，**早失败**）：
      ① 长度上限
      ② 能被 sqlglot(mysql 方言) 解析成**恰好 1 条语句**（多语句 ⇒ 拒绝）
      ③ 不得引用任何表 / 子查询 / SELECT（condition 只应是「列 op 值」的组合）
      ④ 括号正序检查（防提前关闭外层包装括号）
      ⑤ AST 节点类型**白名单**（只允许列引用、字面量、比较/逻辑/白名单函数）

    注意：**不禁 `%` `_` `'` `;` 出现在字符串字面量里**（真实学术文本必需）；
    `;` 之所以有害是因为会产生第 2 条语句 —— 由 ② 精确检出，而非字符黑名单。
    """
    if not cond or not cond.strip():
        return ""

    if len(cond) > max_len:
        raise SqlGuardError(f"condition 过长（{len(cond)} > {max_len}）")

    # 注释检查：**sqlglot 在 tokenize/parse 阶段就把注释丢掉了**
    # （实测 `/*c*/`、`-- x` 都不产生任何 token）⇒ 必须自己扫。
    # 危害：注释留在原样片段里会吃掉我们拼的 `)` / `ORDER BY id LIMIT ?`。
    if _has_comment(cond):
        raise SqlGuardError("condition 中不允许注释（/* */ 、-- （后跟空白）、#）")

    if not _balanced_parens(cond):
        raise SqlGuardError(
            "condition 括号不配平（或出现提前关闭的 ')'）——"
            "后者会截断外层的 AND(...) 分组，使 OR 逃逸"
        )

    if sqlglot is None:
        # 没装 sqlglot 时退回字符级门禁（较弱，但不静默放行）
        logger.warning("sqlglot 未安装：condition 仅做字符级门禁（建议安装）")
        for seq, why in _FORBIDDEN_SEQUENCES:
            if seq in cond:
                raise SqlGuardError(f"condition 中不允许出现 {seq!r}（{why}）")
        return cond

    try:
        stmts = [s for s in sqlglot.parse(cond, read="mysql") if s is not None]
    except Exception as exc:
        raise SqlGuardError(f"condition 不是合法 SQL 表达式：{str(exc)[:120]}")

    if len(stmts) != 1:
        raise SqlGuardError(
            f"condition 必须恰好是 1 条表达式，实际解析出 {len(stmts)} 条"
            "（分号可用来拼接第二条语句 ⇒ 拒绝）"
        )

    tree = stmts[0]

    if list(tree.find_all(exp.Table)):
        raise SqlGuardError("condition 中不允许引用表")
    if list(tree.find_all(exp.Subquery)) or list(tree.find_all(exp.Select)):
        raise SqlGuardError("condition 中不允许子查询 / SELECT")

    bad = sorted({type(n).__name__ for n in tree.walk()} - _ALLOWED_AST_NODES)
    if bad:
        raise SqlGuardError(f"condition 含不允许的语法元素: {bad}")

    return cond


def _balanced_parens(cond: str) -> bool:
    """
    括号健全性：**正序扫描**，任何前缀的 ')' 数不得超过 '(' 数，且最终配平。

    为什么不能只比总数：`1=1) OR (1=1` 的 '(' 与 ')' 各 1 个（总数配平），
    但第一个 ')' 会**提前关闭外层包装括号** ⇒ 拼出的 SQL 变成
    `... AND (1=1) OR (1=1)` ⇒ **OR 逃逸 AND 约束**。
    """
    depth = 0
    for ch in cond:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _has_comment(cond: str) -> bool:
    """
    检测**字符串字面量之外**的 SQL 注释。

    必要性：sqlglot 在 tokenize/parse 阶段就剥掉注释
    （实测 `/*c*/`、`-- x` 均不产生任何 token）⇒ AST 层看不见；
    而注释留在原样片段里会吃掉外层拼的 `)` 与 `ORDER BY ... LIMIT ?`。

    规则（对齐 MySQL）：
      · `/*` 起块注释；`#` 起行注释（MySQL/MariaDB 特有）
      · `--` 仅在**后跟空白 / 控制字符 / 行尾**时才算注释（否则是减号）
      · 单/双引号字符串内一律跳过（`LIKE '%--%'` 不该被误伤）
    """
    i, n = 0, len(cond)
    while i < n:
        ch = cond[i]
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n:
                if cond[i] == "\\":
                    i += 2
                    continue
                if cond[i] == quote:
                    break
                i += 1
            i += 1
            continue
        if ch == "#":
            return True
        if ch == "/" and i + 1 < n and cond[i + 1] == "*":
            return True
        if ch == "-" and i + 1 < n and cond[i + 1] == "-":
            if i + 2 >= n or cond[i + 2] in " \t\r\n":
                return True
        i += 1
    return False


def guard_limit(value: int, lo: int = 1, hi: int = 500) -> int:
    """数值参数夹紧（防意外大 limit 拖库）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise SqlGuardError(f"limit 必须是整数，收到 {value!r}")
    return max(lo, min(hi, n))
