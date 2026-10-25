"""
文件名: melquiades/tokenizer.py
摘要: 共享分词工具。封装 jieba 中文分词，检测 CJK 字符，构建 BOOLEAN MODE 查询，
      以及把查询拆成「线索列表」。所有写入路径与搜索路径使用同一套分词逻辑
      （避免"写入侧/查询侧口径不一致"）。

【COPY 来源】memoria-del-hielo / src/core/tokenizer.py
  取值日期: 2026-09-22

【⚠️ 漂移记录 · 2026-09-22 · 已获用户拍板】
  原纪律「函数逻辑逐行保持一致，便于日后比对漂移」**自本日起解除**。
  用户理由（原话）：「**查书跟查对话索引明显是两个东西**」——memoria 的语料是
  「书名+目录卡片 / 章节 chunk」的书籍，本项目是「对话笔记（均长 1.4KB）+
  压缩摘要（均长 10.7KB）」且在查询形态上是 LLM 手写的多线索串。
  文献亦支持粒度不可平移（arXiv 2212.10423 scope hypothesis；2301.05508）。

  漂移点清单（唯一权威位置，勿散落到 commit message）：
    ① `tokenize_query()` —— 每词**不再加 `+`**（AND ⇒ OR）：
       `+a* +b* +c*` 要求全词命中，对多线索查询实测空手率 86%（draftpad 232 条真实查询，
       200/232 空手）；改为 `a* b* c*` 后空手 0/232。两者 token 集完全相同，唯一变量是算子。
       依据：IR 的 coordination level matching（AND 提精度、毁召回）。
    ② `tokenize_query()` —— 新增 **BOOLEAN MODE 算子消毒**：词内的 `+ - > < ( ) ~ * " @`
       被剔除，两端非 `\\w` 字符被剥掉，清洗后为空则丢弃。
       动因：实测 `%%` 之类线索会让 `AGAINST` 直接抛
       `syntax error, unexpected '+', expecting FTS_TERM`（2/232 查询全文路整路失败）。
    ③ 新增 `split_clues()` —— **优先按空白拆线索**（保留 LLM 自己的分解），
       仅当整串无空白或单段过长时才回落 jieba。
       动因：jieba 会破坏线索边界（`RPC信标`→`RPC`+`信标`；`Зимнее`→逐字母）。
    ④ memoria 侧**未同步**且**不计划同步**（它的语料仍是书）；若日后 memoria 的
       全文路暴露同类问题，本清单可作为参考。

依赖: jieba>=0.42（可选，缺失时降级并告警）
"""

from __future__ import annotations

import re

# CJK 字符范围（Unicode）
_CJK_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3000-\u303f\uff00-\uffef]"
)

# 标准空白分隔（英文/ASCII 文本的分词模式）
_WHITESPACE_RE = re.compile(r"\s+")

# ── CJK 全文检索口径（unigram + bigram）用的「纯汉字」范围 ──────────────
# 与上面的 _CJK_RE 不同：那里含标点/全角符号（用于"这段文本算不算 CJK"的判断），
# 这里只取汉字——标点成 token 会污染索引。
_CJK_HAN = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
# ★ 2026-10-25 修正：原来非 CJK 分支只认 `[A-Za-z0-9_]+`，于是**西里尔文（俄）、
#   希腊文、带变音的拉丁文（é/ü/ñ）整段匹配不到 ⇒ 被直接丢弃，tokenized 列为空。
#   实测触发场景：memorya 的古典学专著里有 Византийская библиотека（拜占庭文库）、
#   Православная Энциклопедия（东正教百科）等成批俄文材料。
#
#   新规则用两段 alternation（**顺序即优先级**）：
#     ① `[CJK]+`  → unigram + bigram（不变）
#     ② `\w+`     → Unicode 模式下匹配所有字母/数字/下划线（俄希拉丁、标识符整体保留）
#   因为 alternation 从左到右，中文不会落进 `\w+`（CJK 也是 \w，但第①段先命中）。
_CJK_CHUNK_RE = re.compile(f"([{_CJK_HAN}]+)|(\\w+)", re.UNICODE)
_CJK_HAN_RE = re.compile(f"[{_CJK_HAN}]")

_jieba_available: bool | None = None


def _check_jieba() -> bool:
    """检查 jieba 是否可导入。结果缓存。"""
    global _jieba_available
    if _jieba_available is not None:
        return _jieba_available
    try:
        import jieba  # noqa: F401
        _jieba_available = True
    except ImportError:
        _jieba_available = False
    return _jieba_available


def has_cjk(text: str) -> bool:
    """检测文本是否包含 CJK（中日韩统一表意文字）字符。"""
    return bool(_CJK_RE.search(text))


def tokenize(text: str) -> str:
    """对文本进行分词，返回空格分隔的 token 字符串。

    - CJK 文本 → jieba 分词（如 jieba 不可用则降级为原始文本）
    - 非 CJK 文本 → 空格分词（不变）
    """
    if not text.strip():
        return ""

    if has_cjk(text):
        if _check_jieba():
            import jieba
            return " ".join(jieba.cut(text))
        else:
            # jieba 未安装：降级——将每个字符单独分，聊胜于无
            import warnings
            warnings.warn(
                "jieba 未安装，CJK 分词降级为逐字符分割。"
                "运行: uv pip install jieba"
            )
            return " ".join(text)
    else:
        # 非 CJK：标准空格分词，去空白归一化
        return " ".join(text.split())


def tokenize_query(query: str) -> str:
    """对搜索查询进行分词，返回 MATCH...AGAINST BOOLEAN MODE 格式。

    CJK 查询 → jieba 分词后每词加 + 和 *（必须包含、前缀匹配）
    非 CJK → 按空格分词后每词加 + 和 *
    """
    if not query.strip():
        return ""

    if has_cjk(query):
        if _check_jieba():
            import jieba
            words = jieba.lcut(query)
        else:
            # 降级：逐字
            words = list(query)
    else:
        words = query.split()

    tokens: list[str] = []
    for w in words:
        cleaned = clean_term(w)
        if not cleaned:
            continue
        # ⚠️ 漂移点①：**不**加 `+`（AND ⇒ OR）。详见文件头漂移记录。
        tokens.append(f"{cleaned}*")

    return " ".join(tokens)


# ──────────────────────────────────────────────
# 漂移点②：BOOLEAN MODE 算子消毒
# ──────────────────────────────────────────────

# 词内出现这些字符会让 MATCH...AGAINST 解析失败或语义反转（`-` 是否定、`+` 是必须、
# `~` 是降权、括号分组、`*` 通配、`"` 短语、`@` 距离、`><` 权重）。
_BOOLEAN_OP_CHARS = str.maketrans({c: " " for c in '+-<>()~*"@%'})

# 词两端的非「词字符」一律剥掉（含全角/中文标点）；`\w` 在 re.UNICODE 下含 CJK 与重音拉丁
_EDGE_NON_WORD = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)


def clean_term(word: str) -> str:
    """把一个原始词清洗成可安全放进 BOOLEAN MODE 的**词干**；清洗后为空则返回空串。

    实测动因：查询里出现 `%%` 这类线索时，旧的 `+%%*` 会让 MariaDB 直接抛
    `syntax error, unexpected '+', expecting FTS_TERM or FTS_NUMB or '*'`
    （232 条真实查询里 2 条整条全文路失败）。清洗把算子换成空格（而非删除），
    这样 `a+b` 会拆成两个词而不是粘成 `ab`。
    """
    if not word:
        return ""
    return _EDGE_NON_WORD.sub("", word.translate(_BOOLEAN_OP_CHARS).strip())


def split_clues(query: str) -> list[str]:
    """把查询拆成**线索列表**（漂移点③）。

    优先**按空白拆**——本项目的查询形态是 LLM 手写的多线索串
    （`"激光 地月 通信 时延 RPC信标 双向"`），空白就是它给出的分解边界。
    只有当整串没有空白、或某一段过长（超过 CLUE_MAX_CHARS）时才回落 jieba。

    为什么不直接用 jieba：它会破坏线索边界——`RPC信标` 被切成 `RPC`+`信标`，
    `Зимнее` 被切成逐字母。而逐线索检索的价值正建立在「线索是 LLM 的原话」上。
    """
    text = (query or "").strip()
    if not text:
        return []
    parts = [p for p in re.split(r"\s+", text) if p]
    if not parts:
        return []
    if len(parts) == 1 or any(len(p) > CLUE_MAX_CHARS for p in parts):
        # 无空白（或存在超长段）⇒ 退回 jieba 分词口径
        parts = split_keywords(text)

    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        cleaned = clean_term(part)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


# 单条线索超过此长度就认为它其实是「一句话」而非一个线索，需要再切
CLUE_MAX_CHARS = 24


# ──────────────────────────────────────────────
# 本项目新增：LIKE 串联（兜底路 / keyword 路）
# ──────────────────────────────────────────────

def split_keywords(query: str) -> list[str]:
    """
    把查询拆成关键词列表，供 LIKE 串联使用。

    CJK → jieba 分词；非 CJK → 空格分词。
    过滤空串与纯标点；保留顺序、去重。
    """
    if not query.strip():
        return []

    if has_cjk(query) and _check_jieba():
        import jieba
        words = jieba.lcut(query)
    elif has_cjk(query):
        words = list(query)
    else:
        words = query.split()

    out: list[str] = []
    seen: set[str] = set()
    for w in words:
        w = w.strip()
        if not w:
            continue
        if len(w) == 1 and not w.isalnum():
            continue
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def escape_like(term: str) -> str:
    """
    转义 LIKE 模式中的特殊字符（\\ % _），供 `LIKE ? ESCAPE '\\\\'` 使用。

    必要性：LIKE 的 % 与 _ 是通配符；用户查询里出现的字面 % 或 _
    （如 "50%"、"a_b"）不加转义会误匹配。
    """
    return (
        term.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


# ══════════════════════════════════════════════════════════════════════════
# CJK 全文检索口径：unigram + bigram（dsh_message 用，**不走 jieba**）
# ══════════════════════════════════════════════════════════════════════════

def tokenize_cjk_ub(text: str) -> str:
    """把文本切成 CJK unigram + bigram 串（写入 FULLTEXT 索引用）。

    规则：
      · 连续汉字串 → **每个汉字**一个 token + **每对相邻汉字**一个 token
        "绘图工具" → 绘 图 工 具 绘图 图工 工具
      · 英文/数字/下划线串 → 原样一个 token
      · 其余字符（标点等）→ 丢弃

    为什么不用 jieba（本模块的 tokenize()）：
      依据 Lucene `CJKBigramFilter(outputUnigrams=true)` 与 Elasticsearch 内置
      `cjk_bigram`；学术侧见 SIGIR'98《Comparing Representations in Chinese
      Information Retrieval》—— bigram 与词切分效果相当甚至更好，且**不依赖词典**。
      jieba 会把专名/新词/代码标识符切错，bigram 是机械规则，切不错。

    为什么必须 unigram（而不是只 bigram）：
      只 bigram 时「绘」搜不到「绘图」——索引里没有 1 字符的 token。
      加上 unigram 才能支持单字查询。

    ★ 前置条件：MariaDB 侧 `innodb_ft_min_token_size = 1`。
      取默认值 3 时 1-2 字符 token 在索引阶段被**静默丢弃**，实测
      「会话」「绘图」「绘」全部 0 命中，而 3 字的「草稿纸」正常。
      （该变量只能改 my.ini 重启生效；`SET GLOBAL` 会成功返回但不生效。）

    ★ 与 tokenize() 的分工：tokenize() 是 jieba 口径，服务 draftpad 旧索引；
      本函数是 dsh_message 的口径。**两者不可混用**——混用会让写入侧与查询侧
      的 token 集合不一致，表现为"明明有却搜不到"。
    """
    if not text:
        return ""

    out: list[str] = []
    for cjk, other in _CJK_CHUNK_RE.findall(text):
        chunk = cjk or other
        if cjk:                                                  # CJK：unigram + bigram
            out.extend(chunk)
            out.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
        else:
            out.append(chunk)
    return " ".join(out)


def build_cjk_boolean(query: str) -> str:
    """把用户查询转成 MATCH...AGAINST 的 BOOLEAN 表达式（写入口径的查询侧镜像）。

    · 单字 CJK → `+字`
    · 多字 CJK → 全部 bigram 用 AND 串起来（"+绘图 +图工 +工具"）
    · 英文/数字 → `+原词`

    与 tokenize_cjk_ub 必须保持同一套切分规则，否则查询侧拼出的 token
    在索引里根本不存在。
    """
    terms: list[str] = []
    for cjk, other in _CJK_CHUNK_RE.findall(query or ""):
        chunk = cjk or other
        if cjk:
            if len(chunk) == 1:
                terms.append("+" + chunk)
            else:
                terms.extend("+" + chunk[i:i + 2] for i in range(len(chunk) - 1))
        else:
            terms.append("+" + chunk)
    return " ".join(terms)
