"""
文件名: melquiades/schema.py
摘要: 表结构演进 —— 幂等迁移。所有 DDL 变更集中于此，便于审阅与回滚。

已实测的三条 MariaDB 12.3 向量约束（2026-09-22，test_db 探针）:
  ① `VECTOR INDEX` **要求列 NOT NULL**（否则 `All parts of a VECTOR index must be NOT NULL`）
  ② 对**已有数据的表**可用 `NOT NULL DEFAULT (VEC_FromText('[...]'))` 一步加列，
     已有行自动获得默认值
  ③ 距离函数是 `VEC_DISTANCE_COSINE`（`VEC_DISTANCE` 会报 "index is not found"），
     且查询须把向量放进**派生表解析一次**：`(SELECT VEC_FromText(?) AS qv) q`
     —— memoria 实测逐行重解析 0.206ms/行 × 161,627 行 = 33 秒

依赖: dbcore.py
"""

from __future__ import annotations

import logging

from melquiades.dbcore import MariaDB

logger = logging.getLogger(__name__)

# 向量维度（BGE-M3 / bge-large-zh 均为 1024）
EMBEDDING_DIM = 1024

# 零向量：作为"尚未计算嵌入"的占位（NOT NULL 约束要求）
ZERO_VECTOR = "[" + ",".join(["0"] * EMBEDDING_DIM) + "]"

# 需要向量列的表：(表名, 索引名)
VECTOR_TABLES = (
    ("draftpad", "idx_vec_draftpad"),
    ("dsh_compaction", "idx_vec_compaction"),
)


def ensure_vector_column(
    db: MariaDB, table: str, index_name: str, dim: int = EMBEDDING_DIM
) -> str:
    """
    为某表确保存在向量列 + VECTOR INDEX（幂等）。

    返回状态字符串（供日志/CLI 展示）：
      "skipped:no-table" / "skipped:already" / "added-column" / "added-index" / "added-both"
    """
    if not db.table_exists(table):
        return "skipped:no-table"

    has_col = db.column_exists(table, "embedding")
    has_idx = db.index_exists(table, index_name)

    if has_col and has_idx:
        return "skipped:already"

    added = []
    if not has_col:
        # 对已有数据的表：NOT NULL + 表达式默认值（实测可行）
        db.execute(
            f"ALTER TABLE {table} ADD COLUMN embedding VECTOR({dim}) NOT NULL "
            f"DEFAULT (VEC_FromText('{ZERO_VECTOR}')) "
            "COMMENT '语义向量（零向量 = 尚未计算嵌入）'"
        )
        added.append("column")

    if not has_idx:
        # DISTANCE / M 必须显式写：省略会**静默降级**为 M=6 + euclidean
        db.execute(
            f"ALTER TABLE {table} ADD VECTOR INDEX {index_name} (embedding) "
            "M=16 DISTANCE=cosine"
        )
        added.append("index")

    if len(added) == 2:
        return "added-both"
    return f"added-{added[0]}"


def ensure_all_vectors(db: MariaDB, dim: int = EMBEDDING_DIM) -> dict[str, str]:
    """为所有需要的表确保向量列 + 索引（幂等）。返回 {表: 状态}。"""
    out: dict[str, str] = {}
    for table, idx in VECTOR_TABLES:
        try:
            out[table] = ensure_vector_column(db, table, idx, dim)
        except Exception as exc:
            out[table] = f"error:{type(exc).__name__}:{str(exc)[:100]}"
            logger.error("ensure_vector_column(%s) failed: %s", table, exc)
    for table in EMBEDDED_TABLES:
        key = f"{table}.embedded"
        try:
            out[key] = ensure_embedded_column(db, table)
        except Exception as exc:
            out[key] = f"error:{type(exc).__name__}:{str(exc)[:100]}"
            logger.error("ensure_embedded_column(%s) failed: %s", table, exc)
    return out


# 需要 `embedded` 标记列的表
EMBEDDED_TABLES = ("draftpad", "dsh_compaction")


def ensure_embedded_column(db: MariaDB, table: str) -> str:
    """
    确保该表存在 `embedded` 标记列（幂等）。

    为什么必须用独立列：MariaDB 的 VECTOR 类型上 `embedding = VEC_FromText(...)`
    与 `!= ...` 比较**实测不可靠** —— 实测 29/103 行零向量既没被检索侧的 `!=`
    排除，也没被 backfill 的 `=` 找到（表现为"零向量参与语义检索，且因 dist=0
    排在最前、拿假最高分"）。因此凡是"这一行是否已嵌入"的判断，一律以本列为准。
    """
    if not db.table_exists(table):
        return "skipped:no-table"
    if db.column_exists(table, "embedded"):
        return "skipped:already"
    db.execute(
        f"ALTER TABLE {table} ADD COLUMN embedded TINYINT(1) NOT NULL DEFAULT 0 "
        "COMMENT '1=已成功嵌入；0=零向量占位（VECTOR 列比较不可靠，以此列为准）'"
    )
    return "added"


def is_zero_vector(vec_literal: str) -> bool:
    """判断向量字面量是否为零向量（用于查询侧过滤）。"""
    return vec_literal.strip() == ZERO_VECTOR


# ──────────────────────────────────────────────
# 受控词表（SKOS 形状的同义词环）—— 2026-09-22
# ──────────────────────────────────────────────

#: 词表表的 DDL。**只存「决定」，不存「事实」**：
#:   标签实际用了多少次、第一次出现在哪一行 —— 这些从 `draftpad.tags` 现算，
#:   不落库。落库只会造出第二份真相源（今天刚在别处栽过）。
#: 语义（对应 SKOS）：
#:   draftpad_vocab        = skos:Concept（一个同义词环一个概念）
#:   .pref_label           = skos:prefLabel
#:   .note                 = skos:scopeNote（判定依据/理由）
#:   draftpad_vocab_label  = 该概念的各个标签，kind=pref/alt（后者即 skos:altLabel）
#: ★ `label` 做主键 ⇒ **一名一概念**（ISO 25964 的 one concept one term）。
#:   库的排序规则是 utf8mb4_uca1400_ai_ci（**大小写/重音/上标不敏感**，实测
#:   `V3`==`v3`、`muérdago`==`muerdago`、`26Al`==`²⁶Al`）⇒ 主键顺带把
#:   「同词异写」也挡掉了，不必自己再写一遍折叠逻辑。
#: ⚠️ 只登记**多成员**的环；没进表的标签＝自成概念（单成员）。这样表小且语义清楚：
#:   表里每一行都是一次"**已决定**它和别的词等价"的记录。
#: 非破坏性：`draftpad.tags` **一个字都不改**，词表只是一层映射。
#: ⚠️ 存成 (表名, DDL) 显式配对，**不要从 DDL 里猜表名** ——
#: 第一版用 `"draftpad_vocab (" in ddl` 判断，结果第二条 DDL 的外键
#: `REFERENCES draftpad_vocab (concept_id)` 里也含这个子串 ⇒ 两条都判成
#: draftpad_vocab（字典同键覆盖，且 label 表根本没建，直到验证才暴露）。
VOCAB_DDL: tuple[tuple[str, str], ...] = (
    ("draftpad_vocab", """
    CREATE TABLE IF NOT EXISTS draftpad_vocab (
      concept_id  CHAR(32)     NOT NULL COMMENT 'skos:Concept 标识（uuid4 hex，稳定）',
      pref_label  VARCHAR(191) NOT NULL COMMENT 'skos:prefLabel（首选词）',
      note        TEXT         DEFAULT NULL COMMENT 'skos:scopeNote：判定依据/理由',
      created_at  TIMESTAMP(6) NOT NULL DEFAULT current_timestamp(6),
      updated_at  TIMESTAMP(6) NOT NULL DEFAULT current_timestamp(6)
                  ON UPDATE current_timestamp(6),
      PRIMARY KEY (concept_id),
      UNIQUE KEY uk_vocab_pref (pref_label)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci
      COMMENT='受控词表概念（SKOS Concept）；只登记多成员同义词环'
    """),
    ("draftpad_vocab_label", """
    CREATE TABLE IF NOT EXISTS draftpad_vocab_label (
      label       VARCHAR(191) NOT NULL COMMENT '字面标签（原始写法）',
      concept_id  CHAR(32)     NOT NULL COMMENT '所属概念',
      kind        VARCHAR(8)   NOT NULL DEFAULT 'alt'
                  COMMENT 'pref=skos:prefLabel / alt=skos:altLabel（**保留不删**：检索入口）',
      note        TEXT         DEFAULT NULL COMMENT 'skos:scopeNote',
      created_at  TIMESTAMP(6) NOT NULL DEFAULT current_timestamp(6),
      PRIMARY KEY (label),
      KEY idx_vl_concept (concept_id),
      CONSTRAINT fk_vl_concept FOREIGN KEY (concept_id)
        REFERENCES draftpad_vocab (concept_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci
      COMMENT='标签→概念映射（一名一概念）；alt 即 non-preferred/入口词'
    """),
)

#: 概念间关系表（SKOS 的 `skos:broader` / `skos:related`）。
#:
#: ★ 为什么需要它，而不是"把包含关系当同义词并掉"：
#:   contain 关系**大多不是同义词**，是上下位（`口径修正` ⊂ `修正`）或关联
#:   （`黑洞物镜` ⊄ `黑洞`，只是相关）。ISO 25964 把 §8.1 **等价**（USE/UF）
#:   与 §10 **层级**（BT/NT）分开处理 —— 混为一谈会造出错误的同义词环。
#:   判据见 ISO 25964-1 §10.2：**下位概念是否完全落在上位概念的范围内？**
#:
#: 语义：`(concept_id, 'broader', target_id)` ⇒ concept_id 的上级是 target_id
#:     （即 SKOS 的 `A skos:broader B`）。反向 `narrower` 由查询推出，不重复存。
#: ★ 允许**单标签概念**：层级的两端常常不是同义词环的成员
#:   （`黑洞` 自己就是一个概念），故本表的存在意味着
#:   `draftpad_vocab` 里可以有只有一个标签的概念。
RELATION_DDL = """
    CREATE TABLE IF NOT EXISTS draftpad_vocab_relation (
      concept_id  CHAR(32)    NOT NULL COMMENT '起点概念',
      rel_kind    VARCHAR(12) NOT NULL COMMENT 'broader=skos:broader / related=skos:related',
      target_id   CHAR(32)    NOT NULL COMMENT '目标概念',
      note        TEXT        DEFAULT NULL COMMENT '判定依据（ISO §10.2 BT/NT 检验等）',
      created_at  TIMESTAMP(6) NOT NULL DEFAULT current_timestamp(6),
      PRIMARY KEY (concept_id, rel_kind, target_id),
      KEY idx_vr_target (target_id),
      CONSTRAINT fk_vr_from FOREIGN KEY (concept_id)
        REFERENCES draftpad_vocab (concept_id) ON DELETE CASCADE,
      CONSTRAINT fk_vr_to FOREIGN KEY (target_id)
        REFERENCES draftpad_vocab (concept_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci
      COMMENT='概念间关系（skos:broader / skos:related）；层级与等价分开存'
"""

#: 标签列宽上限 —— 落库前的守卫线。实测现有 3679 个标签最长 39 字符，
#: 但**绝不静默截断**：超长直接报错（截断＝静默改数据）。
LABEL_MAX_CHARS = 191


def ensure_vocab_tables(db: MariaDB) -> dict[str, str]:
    """建受控词表两张表（幂等）。返回 {表: 状态}。"""
    out: dict[str, str] = {}
    for table, ddl in VOCAB_DDL + (("draftpad_vocab_relation", RELATION_DDL),):
        try:
            if db.table_exists(table):
                out[table] = "skipped:already"
                continue
            db.execute(ddl)
            out[table] = "created"
        except Exception as exc:
            out[table] = f"error:{type(exc).__name__}:{str(exc)[:120]}"
            logger.error("ensure_vocab_tables(%s) failed: %s", table, exc)
    return out
