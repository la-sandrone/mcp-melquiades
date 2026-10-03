"""
文件名: melquiades/vocab.py
摘要: 受控词表（SKOS 形状的同义词环）—— 运行时 + 纯函数工具。

      语义（对应 SKOS，表见 schema.py）：
        draftpad_vocab        skos:Concept
        .pref_label           skos:prefLabel
        draftpad_vocab_label  kind=pref/alt（alt 即 skos:altLabel = non-preferred/入口词）

      ★ 本模块是**归一化与相似度规则的唯一真相源**：构建脚本（tools/replay/build_vocab.py）
        与运行时共用同一套，避免"挑词时按一套规则归一、检索时按另一套"。

      ★ 为什么要多路并用（2026-09-22 实测，见 tools/replay/build_vocab.py 的分档报告）：
        · Jaro-Winkler 是**为拉丁人名设计的**（含打字/拼写变体）。中文短串上实测：
            修正/勘误   JW=0.000（零共享字符）
            上下文恢复/恢复上下文 JW=0.000 ← ★ Jaro 的匹配窗口 = max(len)/2-1，
              5 字串时窗口只有 1.5，**长串倒序它完全看不见**
            修正/修正包  JW=0.911 ← 它真能抓的只有"共享前缀"那类
        · 故中文必须补：**字符多重集**（倒序，对应 ISO 25964-1 §7.7 的倒序形式）、
          **包含关系**（子串）、**字符 Dice**（部分重叠）；嵌入只作末位补充
          （抽象词根对如 修正/勘误 属 ISO 的 near-synonym，**只能人裁决**）。
依赖: rapidfuzz, ./config, ./dbcore
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional

from rapidfuzz.distance import JaroWinkler

logger = logging.getLogger(__name__)

#: 归一化时折叠掉的分隔符（半角 + 全角）。**只折叠，不删除字符本身** ——
#: 归一化结果只用作**分组键**，落库的仍是原始写法。
SEP_RE = re.compile(r"[\s\u3000_\-·・/、,，.。:：;；|]+")

#: 建议档位阈值（**实测标定**，见 tools/replay/out/vocab_build.json 的分布）：
#: 高于 SUGGEST_STRONG 视为"强建议"；低于 SUGGEST_WEAK 不提示。
SUGGEST_STRONG = 0.90
SUGGEST_WEAK = 0.75
#: 各信号**各自的**门槛（单独设，不混比大小）。
#: dice 0.80：中文里"3 字串共享 2 字"或"4 字共享 3 字"即达此值。
#: jw 0.85：中文短串上 jw 的"共享前缀"会虚高，故比 dice 高。
DICE_WARN = 0.80
JW_WARN = 0.85
#: jw 路要求的最小公共前缀长度。实测 `SKOS`/`STKS` jw=0.85（只共享首字母 S）
#: 会命中，人看不出任何相似 —— 这类"首字母巧合同"是提示变吵的主因。
JW_MIN_PREFIX = 2


def _common_prefix(a: str, b: str) -> int:
    ka, kb = norm_key(a), norm_key(b)
    n = 0
    for x, y in zip(ka, kb):
        if x != y:
            break
        n += 1
    return n
#: （已删）曾经用 `score *= 1 + w·ln(1+freq)` 做频次加成 —— 实测会把高频词的
#: 相似度**顶到饱和**（附录C 65 次 ⇒ 把 0.82 顶成 1.00），阈值随即失效。
#: 现改为：**分数只用相似度，频次只用于排序**。


def norm_key(label: str) -> str:
    """归一化分组键：NFKC → casefold → 折叠分隔符 → 去首尾。

    NFKC 顺带解决上标/全角（`²⁶Al` → `26Al`）；casefold 解决大小写。
    注意 MariaDB 侧 `utf8mb4_uca1400_ai_ci` 已能折叠大小写/重音/上标，
    但**不能**折叠分隔符（实测 `Mare_Cordis` != `Mare Cordis`）⇒ 这一步必要。
    """
    s = unicodedata.normalize("NFKC", label or "")
    s = SEP_RE.sub("", s)
    return s.casefold().strip()


def anagram_key(label: str) -> str:
    """字符多重集指纹：**倒序形式**（`上下文恢复`/`恢复上下文`）会得到同一个键。

    对应 ISO 25964-1 §7.7：倒序形式可作为**非首选词**登记。
    这是纯机械规则，不含语义判断 —— 故可进自动档。
    """
    return "".join(sorted(norm_key(label)))


def is_inverted_pair(a: str, b: str) -> bool:
    """是否互为倒序形式（同字符多重集 + 长度≥2 + 字面不同）。

    ★ 对中文而言这只是**线索不是判据**（2026-09-22 用户裁决）：
      中文的**动宾倒装**是能产构词法（`守灯`=动作 ↔ `灯守`=职守），
      造出的词**字符集必然相同但词性不同** ⇒ 倒序检测在中文上天然高误报。
      实测 7 组倒序里 4 组是同形异义/异词性：Ar/Ra（氩/瑞利数）、PAL/PLA
      （电视制式/解放军）、冬至/至冬（节气/地区名）、守灯/灯守（动/名）。
      ⇒ 故倒序**永不自动合并**，只能提示 + 请人确认。
    """
    ka, kb = norm_key(a), norm_key(b)
    return len(ka) >= 2 and ka != kb and anagram_key(a) == anagram_key(b)


#: 尾部 ASCII 字母数字串（用于"并列系列"检测）
TAIL_ALNUM_RE = re.compile(r"[0-9A-Za-z]+$")


def is_parallel_series(a: str, b: str, min_stem: int = 2) -> bool:
    """是否**并列系列的两个实例**（`附录H`/`附录C`、`O5-1`/`O5-13`）⇒ **不是同义词**。

    判据：抹掉尾部那段 ASCII 字母数字后，主干相同（且主干至少 min_stem 个字符）。
    这类标签是同一系列的成员，互相"像"但**语义上并列、不可互换**。

    ★ 为什么必须有这条：实测 `附录H` 会向 `附录C(65)`/`附录F(15)` 提示"相似 1.00"，
      而 附录H 是完全正当的独立概念 —— 这种提示就是狼来了。
      （数字版的同款规则见 build_vocab.py 的 is_same_numbered_series。）
    """
    stem_a = TAIL_ALNUM_RE.sub("", a)
    stem_b = TAIL_ALNUM_RE.sub("", b)
    if len(stem_a) < min_stem or len(stem_b) < min_stem:
        return False
    return stem_a == stem_b and a != b


def contains(a: str, b: str) -> bool:
    """a 是否为 b 的子串（归一化后）。`修正` ⊂ `修正包`。"""
    ka, kb = norm_key(a), norm_key(b)
    return bool(ka) and ka != kb and ka in kb


def char_dice(a: str, b: str) -> float:
    """字符多重集的 Dice 系数 —— 中文短串的部分重叠信号。

    `裁决`/`裁定` = 2*1/(2+2) = 0.5；`修正`/`勘误` = 0（零共享字符，正确）。
    """
    ca, cb = Counter(norm_key(a)), Counter(norm_key(b))
    if not ca or not cb:
        return 0.0
    inter = sum((ca & cb).values())
    return 2.0 * inter / (sum(ca.values()) + sum(cb.values()))


def jw(a: str, b: str) -> float:
    """Jaro-Winkler 相似度（rapidfuzz，已用 Winkler 论文标准例验证）。"""
    return float(JaroWinkler.similarity(a, b))


def similarity(a: str, b: str) -> dict:
    """多路相似度明细 —— 便于报告里看清"是哪条路抓到的"。"""
    return {
        "norm_eq": norm_key(a) == norm_key(b),
        "inverted": is_inverted_pair(a, b),
        "contain": contains(a, b) or contains(b, a),
        "dice": round(char_dice(a, b), 4),
        "jw": round(jw(a, b), 4),
    }


# ──────────────────────────────────────────────
# 标签使用现状（**事实**，现算，不落库）
# ──────────────────────────────────────────────

@dataclass
class TagIndex:
    """全库标签的使用现状：标签 → (出现次数, 首现行 id)。

    ★ 只读 `draftpad.tags` 现算，**不落库** —— 落库会造出第二份真相源。
    """

    freq: dict[str, int] = field(default_factory=dict)
    first_id: dict[str, int] = field(default_factory=dict)
    #: 预计算的归一化键 / 字符倒排 —— 建议时先按"共享至少一个字符"预筛，
    #: 把 3679 次 similarity()（每次含 NFKC+Counter+JW）降到几十次。
    _nk: dict[str, str] = field(default_factory=dict)
    _chars: dict[str, list[str]] = field(default_factory=dict)
    _loaded_at: float = 0.0

    @property
    def size(self) -> int:
        return len(self.freq)

    @classmethod
    def from_db(cls, db) -> "TagIndex":
        import json
        idx = cls()
        rows = db.query(
            "SELECT id, tags FROM draftpad WHERE deleted_at IS NULL "
            "AND tags IS NOT NULL AND tags != '[]' ORDER BY id", readonly=True)
        for r in rows:
            try:
                ts = json.loads(r["tags"] or "[]")
            except Exception:
                continue      # 畸形 tags（实测 21 行）：跳过但另有统计，不静默当 0
            if not isinstance(ts, list):
                continue
            for t in ts:
                t = str(t).strip()
                if not t:
                    continue
                idx.freq[t] = idx.freq.get(t, 0) + 1
                idx.first_id.setdefault(t, int(r["id"]))
        for t in idx.freq:
            k = norm_key(t)
            idx._nk[t] = k
            for ch in set(k):
                idx._chars.setdefault(ch, []).append(t)
        idx._loaded_at = time.monotonic()
        return idx

    def candidates_sharing_char(self, label: str) -> list[str]:
        """预筛：与 label **共享至少一个归一化字符**的已有标签。

        中文里这是 dice>0 与 jw>0 的必要条件（`钦定`/`月球` 共享 0 字符 ⇒ 直接排除）。
        """
        out: set[str] = set()
        for ch in set(norm_key(label)):
            out.update(self._chars.get(ch, ()))
        return list(out)

    def top(self, n: int = 0) -> list[tuple[str, int]]:
        items = sorted(self.freq.items(), key=lambda kv: (-kv[1], kv[0]))
        return items[:n] if n else items


# ──────────────────────────────────────────────
# 词表（SKOS 同义词环）
# ──────────────────────────────────────────────

@dataclass
class Ring:
    concept_id: str
    pref_label: str
    labels: list[str]                      # 含 pref 自己
    note: str = ""


class Vocabulary:
    """受控词表内存视图：概念（含**单标签概念**）+ 标签 + 概念间关系。

    ★ 概念不一定是同义词环：层级的两端常常是单标签概念（`黑洞` 自己就是一个概念）。
      故 `concepts` 是全部概念，`rings` 只是其中**多标签**的那些（等价关系）。
    """

    def __init__(self, concepts: list[Ring],
                 relations: Iterable[tuple[str, str, str]] = ()) -> None:
        self.concepts = concepts
        # 字面标签 → 该概念全部标签（含自身）。键同时按原样与归一化键登记，
        # 这样 `Mare_Cordis` 与 `Mare Cordis` 都能查到同一个概念。
        self._by_label: dict[str, list[str]] = {}
        self._cid_of: dict[str, str] = {}
        for r in concepts:
            for lb in r.labels:
                self._by_label[lb] = r.labels
                self._by_label.setdefault(norm_key(lb), r.labels)
                self._cid_of[lb] = r.concept_id
                self._cid_of.setdefault(norm_key(lb), r.concept_id)
        self._by_cid = {r.concept_id: r for r in concepts}
        self.relations: list[tuple[str, str, str]] = list(relations)
        self._broader: dict[str, list[str]] = {}
        self._narrower: dict[str, list[str]] = {}
        for a, kind, b in self.relations:
            if kind == "broader":
                self._broader.setdefault(a, []).append(b)
                self._narrower.setdefault(b, []).append(a)

    @property
    def rings(self) -> list[Ring]:
        """**多标签**概念（= 同义词环）。保留旧名以免破坏调用方。"""
        return [c for c in self.concepts if len(c.labels) > 1]

    @property
    def ring_count(self) -> int:
        return len(self.rings)

    @property
    def concept_count(self) -> int:
        return len(self.concepts)

    @property
    def label_count(self) -> int:
        return sum(len(c.labels) for c in self.concepts)

    @property
    def relation_count(self) -> int:
        return len(self.relations)

    @classmethod
    def load(cls, db) -> "Vocabulary":
        """从库读词表。表不存在时返回空词表（**不报错**：词表是增量建的）。"""
        if not db.table_exists("draftpad_vocab"):
            logger.info("vocab 表不存在，返回空词表")
            return cls([])
        rows = db.query(
            "SELECT v.concept_id, v.pref_label, v.note, l.label "
            "FROM draftpad_vocab v JOIN draftpad_vocab_label l "
            "  ON l.concept_id = v.concept_id ORDER BY v.pref_label, l.label",
            readonly=True)
        grouped: dict[str, dict] = {}
        for r in rows:
            g = grouped.setdefault(str(r["concept_id"]), {
                "pref": str(r["pref_label"]), "note": str(r.get("note") or ""),
                "labels": []})
            g["labels"].append(str(r["label"]))
        concepts = [Ring(cid, g["pref"], sorted(g["labels"]), g["note"])
                    for cid, g in grouped.items()]
        rels: list[tuple[str, str, str]] = []
        if db.table_exists("draftpad_vocab_relation"):
            for r in db.query("SELECT concept_id, rel_kind, target_id "
                              "FROM draftpad_vocab_relation", readonly=True):
                rels.append((str(r["concept_id"]), str(r["rel_kind"]),
                             str(r["target_id"])))
        return cls(concepts, rels)

    def siblings(self, label: str) -> list[str]:
        """该标签所属概念的**全部标签**（含自身）；不在词表里则返回 [label]。"""
        hit = self._by_label.get(label) or self._by_label.get(norm_key(label))
        return hit if hit else [label]

    def expand(self, label: str) -> list[str]:
        """等价扩展：同一概念的**其它**标签（含同义词环成员）。"""
        sib = self.siblings(label)
        return [x for x in sib if norm_key(x) != norm_key(label)]

    def _labels_of_cids(self, cids: Iterable[str]) -> list[str]:
        out: list[str] = []
        for cid in cids:
            c = self._by_cid.get(cid)
            if c:
                for lb in c.labels:
                    if lb not in out:
                        out.append(lb)
        return out

    def broader_of(self, label: str) -> list[str]:
        """上位概念的标签（`克尔黑洞` → `黑洞`）。ISO 25964-1 §10 BT/NT。"""
        cid = self._cid_of.get(label) or self._cid_of.get(norm_key(label))
        return self._labels_of_cids(self._broader.get(cid or "", []))

    def narrower_of(self, label: str) -> list[str]:
        """下位概念的标签（`黑洞` → `克尔黑洞`）。"""
        cid = self._cid_of.get(label) or self._cid_of.get(norm_key(label))
        return self._labels_of_cids(self._narrower.get(cid or "", []))

    def expand_hierarchy(self, label: str, *, narrower: bool = True,
                         broader: bool = False) -> list[str]:
        """层级扩展。默认**只向下**（查 `黑洞` 连带 `克尔黑洞`）。

        为什么不默认向上：向上扩展会把结果泛化（查 `克尔黑洞` 混进全部黑洞资料），
        精度掉得比召回赚的多。方向上/下由调用方按场景选。
        """
        out: list[str] = []
        if narrower:
            out += self.narrower_of(label)
        if broader:
            out += self.broader_of(label)
        cur = set(self.siblings(label))
        return [x for x in out if x not in cur]



# ──────────────────────────────────────────────
# 写入侧建议
# ──────────────────────────────────────────────

@dataclass
class Suggestion:
    label: str
    score: float
    freq: int
    why: str
    #: "equiv" = 疑似等价（同义/变体/倒序）⇒ 建议复用已有标签
    #: "hier"  = 包含关系 ⇒ **这是层级（上下位），两者可共存**，只作告知
    #: ★ 分开的理由：ISO 25964 把 §8.1 等价与 §10 层级分开处理；
    #:   把层级也说成"建议复用"就是把两件事混了（我已在 T3 上犯过一次同样的错）。
    kind: str = "equiv"


def suggest_labels(proposed: str, index: TagIndex, *, k: int = 3,
                   strong: float = SUGGEST_STRONG,
                   weak: float = SUGGEST_WEAK) -> list[Suggestion]:
    """给一个拟写标签，找出**已存在的近邻**（写入侧提示用，**不阻断写入**）。

    依据（三方汇聚）：
      · ISO 25964-2 §13.2.2(d)：精确等价可自动，其余需人介入
      · Quattrone CIKM'11 §2.2：建议时**偏向高频标签**（count × IRF）
      · obsidian-ai-tag-curator 实践：**优先复用已有标签**，且要**同时列出未选中的近似候选**

    打分 = 多路相似度取最大 × 频次权重（高频优先）。**只报候选，不替人决定。**
    """
    if not proposed.strip() or index.size == 0:
        return []
    out: list[Suggestion] = []
    suppressed = 0
    for lab in index.candidates_sharing_char(proposed):
        if lab == proposed:
            continue
        if is_parallel_series(proposed, lab):
            suppressed += 1        # 并列系列（附录H/附录C）：明确抑制，不提示
            continue
        f = index.freq[lab]
        sims = similarity(proposed, lab)
        # ★ **每个信号各定自己的门槛**，不再混成"取最高分"：
        #   中文短串上 jw 的"共享前缀"会虚高（`附录H`/`附录文体` = 0.78），
        #   若和 dice 混在一起比大小，就会把前缀巧合抬进建议里。故：
        kind = "equiv"
        if sims["norm_eq"]:
            score, why = 1.0, "同词异写（归一化后相同）"
        elif sims["inverted"]:
            # 倒序形式**写作问句而非断言**：实测 7 组倒序里 3 组是真同义、
            # 3 组是同形异义（Ar/Ra、PAL/PLA、冬至/至冬）⇒ 只能请人确认。
            score, why = (0.95, "字符相同仅顺序不同 —— **请确认是否同一概念**；"
                                 "中文动宾倒装可能是**故意的词性对立**"
                                 "（如 `守灯`=动作 / `灯守`=职守），"
                                 "这类**不该**合并（ISO §7.7 允许登记为入口词）")
        elif sims["contain"]:
            # 包含关系 = **层级**（上下位），不是等价 ⇒ 两者可以共存，
            # 故归 hier 类，措辞只做告知，不推"复用"。
            score, why = 0.92, ("包含关系（一者是另一者的子串）—— 属**层级/上下位**"
                                "（ISO §10），两者可并存")
            kind = "hier"
        elif sims["dice"] >= DICE_WARN:
            score, why = sims["dice"], f"字符高度重叠（Dice={sims['dice']:.2f}）"
        elif sims["jw"] >= JW_WARN and _common_prefix(proposed, lab) >= JW_MIN_PREFIX:
            # ★ 要求公共前缀 ≥2：否则 SKOS/STKS 这种"首字母巧合同"也会命中
            #   （jw=0.85 但人类看不出来），实测就是它把提示搞吵的。
            score, why = sims["jw"], "拼写相近（共享前缀/编辑距离近）"
        else:
            continue
        out.append(Suggestion(lab, round(score, 4), f, why, kind))
    # ★ 频次**只用于排序**，不进分数。
    #   原写法 `score *= 1+w·ln(1+freq)` 会把高频词的相似度顶到饱和
    #   （`附录H` → `附录C`(65 次) 被顶成 1.00），阈值就失去意义了。
    #   Quattrone CIKM'11 eq.9 那个乘积是**排序候选**用的，不是阈值判据。
    out.sort(key=lambda s: (-s.score, -s.freq, s.label))
    if suppressed:
        logger.info("suggest(%r)：抑制 %d 个并列系列候选", proposed, suppressed)
    return out[:k]


#: 查询扩展最多并入多少个同义词（防病态：某个环极大或查询含很多环内词）
MAX_EXPAND_TERMS = 12


def expand_query_terms(vocab: Vocabulary, query: str, *,
                       limit: int = MAX_EXPAND_TERMS,
                       hierarchy: bool = True,
                       narrower: bool = True,
                       broader: bool = False,
                       ) -> tuple[list[str], list[str], dict]:
    """从查询串里认出词表内的标签，返回 (要并入的词, 命中的原词, 明细)。

    两条扩展路（ISO 25964-2 原文：「facilitate choice of terms and/or expansion of
    search expressions to include terms for **broader, narrower or related concepts,
    as well as synonyms**」）：
      · 等价（同义词环）—— 默认开：查 `裁决` 并入 `裁定`
      · 层级（下位）—— 默认开、**只向下**：查 `黑洞` 并入 `克尔黑洞`
        （向上会把结果泛化，精度掉得比召回赚的多；故 broader 默认关）

    切分：先试**整串**（有人会把 `待裁决` 当一条线索写），再按空白切。
    明细 dict 供上层**写进 notes 留痕**（必须能看出哪些词是哪条路来的）。
    """
    detail = {"equiv": [], "narrower": [], "broader": [], "matched": []}
    if not query.strip() or vocab.concept_count == 0:
        return [], [], detail
    probes = [query.strip()] + [p for p in re.split(r"\s+", query.strip()) if p]
    extra: list[str] = []
    for p in probes:
        hit = False
        for x in vocab.expand(p):                      # 等价路
            hit = True
            if x not in extra:
                extra.append(x)
                detail["equiv"].append(x)
        if hierarchy:
            for key, want in (("narrower", narrower), ("broader", broader)):
                if not want:
                    continue
                got = vocab.expand_hierarchy(p, narrower=(key == "narrower"),
                                             broader=(key == "broader"))
                for x in got:
                    hit = True
                    if x not in extra:
                        extra.append(x)
                        detail[key].append(x)
        if hit and p not in detail["matched"]:
            detail["matched"].append(p)
    return extra[:limit], detail["matched"], detail


def format_suggestions(proposed: str, sugs: list[Suggestion],
                       also_submitted: Optional[set] = None) -> str:
    """人可读的建议文本。**等价与层级分成两段，措辞不同**。

    - 等价（同义/变体/倒序）→ 「建议优先复用已有标签」
    - 层级（包含关系）      → 「已有上位/下位词，**两者可并存**」（只告知）

    `also_submitted`：本次**同批提交**的其它标签；若候选已在其中，
    说明两个词你本来就都写了 ⇒ 提示冗余，直接抑制（实测 5 个标签弹 4 条的主因）。
    """
    if not sugs:
        return ""
    if also_submitted:
        sugs = [s for s in sugs if s.label not in also_submitted]
    if not sugs:
        return ""
    equiv = [s for s in sugs if s.kind == "equiv"]
    hier = [s for s in sugs if s.kind == "hier"]
    lines: list[str] = []
    if equiv:
        head = ("⚠️ 标签近邻提示：" if any(s.score >= SUGGEST_STRONG for s in equiv)
                else "提示：")
        lines.append(f"{head}拟写的「{proposed}」在词表里已有**疑似等价**的标签——")
        lines.append("   建议**优先复用已有标签**（受控词表一致性）。"
                     "若确要新建，照常写入即可。")
        for s in equiv:
            lines.append(f"   · {s.label}（已用 {s.freq} 次）  相似 {s.score:.2f}  {s.why}")
        lines.append("   注：等价与否需人工裁决（ISO 25964 准同义词无唯一正解）。")
    if hier:
        lines.append(f"ℹ️ 「{proposed}」与已有标签存在**层级（上下位）关系**"
                     "——这不是重复，**两者可以并存**：")
        for s in hier:
            lines.append(f"   · {s.label}（已用 {s.freq} 次）  相似 {s.score:.2f}  {s.why}")
        lines.append("   想要的话可登记为 skos:broader（层级），"
                     "登记后查询可沿层级扩展下位词。")
    return "\n".join(lines)
