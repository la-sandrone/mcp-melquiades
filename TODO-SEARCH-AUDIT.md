# 检索同源缺陷审计清单（melquiades 侧）— **已收口 2026-11-07 晚**

> 由来：2026-11-07 在 **memoria-del-hielo** 做垂直分表那一轮里，把「搜不出来」拆成
> 七个可测机制并逐条治掉。两个项目共用同一套 CJK 检索口径，所以这边大概率有同款。
>
> **本清单已执行完毕**：A1–A8 逐条实测 → 改 → 验收。结论与证据都在下面；
> 决策台账见 `DESIGN.md` #30，坑见 `NOTES.md`，变更明细见 `CHANGELOG.md`。
>
> 复跑入口：
> ```bash
> /home/Castorice/ai-services/bin/python3 tools/bench_search.py --build      # 重建金标
> /home/Castorice/ai-services/bin/python3 tools/bench_search.py --legacy     # 改造前口径
> /home/Castorice/ai-services/bin/python3 tools/bench_search.py             # 现口径
> /home/Castorice/ai-services/bin/python3 tools/bench_search.py --precision --legacy
> /home/Castorice/ai-services/bin/python3 tools/bench_search.py --precision
> /home/Castorice/ai-services/bin/python3 -m pytest tests/test_tokenizer.py -q
> ```

---

## 结果一览

| 项 | 判定 | 处置 |
|---|---|---|
| A1 停词表内容 | **成立**（实际 49 个单字，文档写 62） | 收缩到 9 个 |
| A2 匹配粒度 | **成立**（结构性根因） | 子串替换 → token 相等过滤 |
| A3 剪枝可见性 | **成立**（缺） | `stats.query_terms` + 剪枝率告警 |
| A4 未剪重试 | **成立**（缺） | 全文 0 命中 → 未剪表达式重打一次 |
| A5 LIKE 触发条件 | **成立**（每次都跑，实测 2–4 s） | 改成「全文全空」才跑 |
| A6 世代一致性 | **不成立**（已闭） | 抽样 200 行，v2 重算 0 处不一致 |
| A7 None 排序崩 | **不成立**（无同款） | `-x.get(...)` 全仓 0 命中 |
| A8 侧表孤儿 | **不成立**（0  orphan） | 题面 SQL 键写错（见下）；无删除路径 |

### 同一把尺子的改前/改后

| 基准 | legacy | current |
|---|---|---|
| precision@20（25 个含停词字的词） | **44.7%**（219/490） | **93.2%**（466/500） |
| 12 条金标 @5/@10/@20/MRR | 9 / 9 / 10 / 0.653 | 9 / 9 / 10 / 0.653 |
| 12 条金标平均耗时 | 774 ms | **261 ms** |

⚠️ **金标那 12 条对这个改动免疫**（关键词都不含停词字）。差点因此误判「改动无效」——
见 `DESIGN.md` #30「尺子问题」。

---

## A1 · 停词表内容（成立 → 已改）

- **实测**：`SearchConfig().stopwords` 共 117 条，其中 **49 个单字**（原文写 62，口径错）。
  用审计文档自己列的 28 个真词复跑：**28/28 全被切碎**（地质→`质`、太阳→`阳`、过程→`程`）。
- **改法**：单字只留 `的了吗呢吧啊呀哦嗯`；40 个高频构词成分移出。多字虚词
  （什么/为什么/如何/哪些/是不是/算不算…）**全留** —— 它们是整词，不会切碎别人。
- **注意别过度修正**：那边记录的「停词表仍必需（8/8 vs 7/8）」是**排序**收益，
  与这次的**召回**损失是两件事，不矛盾。
- **验收**：`test_no_stopword_fragments_real_words` + `test_stopword_list_has_no_high_frequency_builders`。

## A2 · 匹配粒度：子串替换 → token 层过滤（成立 → 已改，本轮唯一改召回的项）

- **病根**：`tokenizer.split_by_stopwords` 用 `s.replace(w, "\x00")` 做**任意位置**子串替换，
  切完再切 bigram ⇒ token 边界被改。跨边界错位是**双向**的：查询有 `命算`、索引也有 `命算`，
  但查询被切块后不再产出它。
- **改法**：写入与查询共用 `iter_chunks`；停词只做 **token 相等**过滤。
  跨词边界噪声仍在**多字**停词上抑制（标死其覆盖的字位，不让 bigram 跨它拼接）——
  旧实现的初衷保住了，伤害没了。`split_by_stopwords` 已删除（无其它调用点）。
- **判据修正（重要）**：原文的 `query_token ⊆ index_token` **测不出这个 bug**——
  切碎后查询只剩单字，单字本来就在索引里，这条不变量在旧实现上是**绿的**。
  真判据是「真词必须产出自己的 bigram」。
- **验收**：`test_no_stopword_fragments_real_words`、`test_query_tokens_are_subset_of_index_tokens`（保留为护栏）、
  `test_multichar_stopword_does_not_leak_boundary_bigrams`、`test_single_char_query_never_empties`、
  `test_all_stopword_query_falls_back_to_unigrams`。

## A3 · 剪枝的可见性（成立 → 已补）

- `search()` 的 stats 新增 `query_terms: {source: {query_expr, kept[], pruned[], retried_unpruned}}`；
  剪掉的词数 ≥ 留下的词数时 `logger.warning`。
- 为什么先补 A4 再补 A3：顺序不能反 —— **先有保险（A4），再暴露内部细节（A3）**。

## A4 · 0 命中时的「未剪重试」层（成立 → 已补）

- 全文路剪枝后 0 命中 ⇒ 用不套停词的表达式再打一次 ⇒ 仍 0 才轮到 LIKE。
- 理由：剪枝只许削弱精度，绝不许影响「能不能捞到」；NL 重试是秒级，LIKE 是秒~十几秒。

## A5 · LIKE 兜底的触发条件（成立 → 已改判据）

- **实测**：旧实现 `plan.append(("like", ...))` 无条件下挂 —— 每次查询都跑。
  本库 194,325 行 / 442 MB body：`%量子退火%` **2.01 s**、缺席串 **3.94 s**、命中即停 0.03 s。
- **判据修正**：原文写「全文 + 向量都空」，但**向量路没有阈值**，任何查询都返回 top-k
  ⇒ 按原判据 LIKE 永远不可达 = 静默删掉一条路。改成「**全文全空**」。
- **实测代价**：缺席查询 190 ms → 2.9 s（只在这种查询上付）。
- 权重继续 0.05（无相关性信号）。

## A6 · 分词口径的世代一致性（**不成立：已闭**）

- 写入侧 `harvest/message_runner.py` 用的就是 `tokenize_cjk_ub_v2`（主表 + `_fts` 侧表都写）。
- **复跑**：随机抽 200 行，用 `tokenize_cjk_ub_v2(body)` 重算与库里 `body_tokenized` 比对 ——
  **0 处不一致**。

## A7 · 排序键遇到 None 会崩（**不成立：无同款**）

- `omnibus.py` 用 `sorted(merged.values(), key=lambda x: -x.score)`（对象属性，非 None）。
- 全仓扫 `-x.get(...)` 模式：0 命中。仅剩几处无害的 `sorted(..., key=lambda d: d.get("index", 0))`
  （`tools/replay/` 的一次性脚本）。

## A8 · 侧表无外键 ⟹ 应用层级联（**不成立：0 orphan**）

- **题面的复跑 SQL 键写错了**：侧表主键是 `guid`，不是 `id`；照写 `v.id = m.id` 直接
  `Unknown column 's.id'`。正确写法：

  ```sql
  SELECT COUNT(*) FROM (
    SELECT f.guid FROM dsh_message_fts f LEFT JOIN dsh_message m ON m.guid = f.guid
    WHERE m.guid IS NULL) t;   -- 0
  ```

- 实测：`dsh_message_fts` 0 条孤儿，`dsh_message_vec` 0 条孤儿。
- 全仓 `DELETE FROM dsh_message` / `DROP TABLE` / `TRUNCATE`：0 处 —— 目前**没有删除路径**，
  所以没有 FK 也不会攒孤儿。这条留作护栏：**将来加删除路径时，必须显式删三张表**。

---

## 一句话原则（两边通用）

**查询侧的切分口径必须与写入侧逐字一致；任何改变 token 边界的操作（切块、删字、替换），
都会制造「索引里有却查不到」或「查询词不在索引里」的错位 —— 而这类错位永远不报错。**

补一句这一轮学到的：**改召回之前先问「我手上这把尺子量得到它吗」**。
量不到的指标「没变化」既不是成功也不是失败，是**没测**。

---

## 遗留（不属于本清单）

- 仍失败的 2 条金标：`硅基生命`（第 28）、`Bartlett 是不是 化学家`（第 15）。
  与停词无关，是 NL 的 OR 语义下高频词淹没稀有词 —— 属**排序**问题，另开一轮。
- `地球化学` 6/20、`真空层` 0/20：OR 语义下命中了词的一部分（地球 / 真空），
  不是 token 边界问题。要治得靠短语查询（`"地 球 化 学"`）或重排，属下一轮。
