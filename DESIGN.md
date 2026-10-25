# melquiades 设计稿 v0.1

**主题**：草稿纸（跨对话账本） + 压缩档案（DSH 压缩收割）
**状态**：待审（本稿只出文档，不含代码改动）
**日期**：2026-09-22
**取代**：`README.md` 中的"会话内草稿纸"设计（见 §1 病理）

---

## 0. 一页纸摘要

### 三句话

1. **旧设计的病根**：把草稿纸定位成"会话内工作台"（进程级 UUID 隔离、随机 note_key、旧号只读），而真实需求是"跨对话项目账本"——两者方向相反，于是它被整体绕过（984 次裸 SQL / 0 次工具调用）。
2. **新定位**：单库账本 + **五维寻址**（id / created_at / tags / note_key / content），不再按会话隔离。
3. **新增能力**：**收割** DSH 的压缩产物（摘要 + 遮蔽 seq 指针 + 防漏网层）入库，使"刚被压掉的内容"在被遗忘的同一会话内就可检索回来。

### 四条铁律（违反即返工）

| # | 铁律 | 来源 |
|---|---|---|
| **铁律一** | **MariaDB 无特殊理由绝不用长连接**——用完即烧。理由：连接坏了捞不回来，且会占满连接池 | 本项目 & memoria-del-hielo 的血教训 |
| **铁律二** | **复用代码一律 copy 进本项目**，禁止跨项目 import。melquiades 与 memoria-del-hielo 是两个独立项目 | 架构隔离 |
| **铁律三** | **所有魔法数字进配置**，禁止函数体内裸写 | 八荣八耻 |
| **铁律四** | **不静默降级**——失败必须落日志表或抛错，禁止吞错返回空值 | 八荣八耻 |

### 三个最重要的决定

| 决定 | 选择 | 反方意见与处置 |
|---|---|---|
| 收割 vs 接管 | **只收割，不进 DSH 进程** | auto-memory 选择"压缩前拦截"（进进程）；但我们的目标是"压缩后的产物"，不需要付进程内的代价。见 §4.2 |
| 并发安全 | **纯条件 UPDATE（CAS），不用 `INSERT ... ON DUPLICATE KEY UPDATE`** | 实测两者都能跑通（800 次并发无死锁），但后者有已知死锁倾向（MDEV-17073/17603）与锁升级路径。选**原理上无死锁**的写法。见 §4.5 |
| 检索 | **FULLTEXT(jieba) + 向量 两路都做，且保留 LIKE 通道** | 有数据：`MATCH()` 0 次但 `LIKE` 169 次（含 29 次数值探针，FULLTEXT/向量都无法替代）。见 §5 |

---

## 1. 病理：旧设计为什么被绕过

### 1.1 事实基础

| 证据 | 数据 |
|---|---|
| `melquiades_medicamento` 表实况 | `draftpad` 827 行；`draftpad_audit` **0 行**；`draftpad_vectors` **0 行**；`scratch_notes` 8 行（裸写 SQL 时猜错表名的副作用） |
| 真实调用（3 场 DSH 会话） | 984 次 `mcp__mcp-mariadb__execute_sql`，**全部打 melquiades_medicamento**；**0 次** melquiades 工具调用 |
| 为何 0 次 | **melquiades 从未注册进 DSH**——用户主动决定（理由："不大好用，找不到之前的东西"） |
| 工具唯一一次真实使用 | 2026-07：19 条随机 `note_key`（`^[0-9a-f]{12}$`）——之后全部转裸 SQL |
| 主力账本 | `session_id='bdd1982f-ice-planet'` **750 行 / 37 天 / 跨 59·60·61 三场会话** |
| 检索方式 | `MATCH()` **0 次**；`LIKE` **169 次**（占 SELECT 的 33%）；`id >= N` 游标扫 ~338 次 |
| 修订方式 | `UPDATE ... SET content=CONCAT(content,'\n\n…')` 追加指针，批量改 9 行；`ON DUPLICATE KEY UPDATE` 仅 12 次 |
| 自建引用体系 | 正文里遍布 `见 id624`、`替代 id603`、`接id765` |

### 1.2 八条设计假设 vs 现实

| # | 设计假设 | 现实 | 后果 |
|---|---|---|---|
| 1 | 会话 = 进程边界，UUID 藏内存、**LLM 不知道** | 跨对话必须找回笔记 ⇒ 需要**稳定、可读、跨进程不变**的标识 | 用户手写 `bdd1982f-ice-planet` 撑 750 行——**"开新对话找不到之前的东西"的根因** |
| 2 | 写操作不收 GUID，物理上不能跨会话写 | 要往同一条账本继续追加 | 裸 SQL 绕过 |
| 3 | `note_key = uuid4().hex[:12]` 随机 | LLM 需要可读、可定位、可覆盖的 key | 随机 key 只活 19 条；LLM 自建 `【口径修正·2026-09-16】…` 命名法 |
| 4 | 搜索 = FULLTEXT + VECTOR | 真实需求是**顺序浏览 + 增量游标**（"我写到哪了"）；模糊查找靠 LIKE | `MATCH` 0 次；`draftpad_vectors` 0 行 |
| 5 | 审计日志记 DELETE/UPDATE | 涂改靠 `CONCAT` 追加（原文不动） | `draftpad_audit` 0 行；追加式修订**本身就是**最好的审计 |
| 6 | 8 个语义化工具 | 需要**列清单 / 按 id 游标增量读 / 批量 CONCAT 追加** | 三个能力全缺 ⇒ 工具被整体弃用 |
| 7 | `switch_session` 换号，旧号只读 | 换号 = 主动遗弃记忆 | 从未使用 |
| 8 | 跨会话搜索需显式传 GUID | GUID 不可见、不可持久 | 实际等价于"永久失联" |

### 1.3 一句话

> 设计的心智模型是「**会话内工作台**」（短期、易失、隔离、语义检索）；
> 真实需求是「**跨对话项目账本**」（长期、累积、按序可寻址、追加式修订）。

**关键澄清**：草稿纸机制**确实解决了问题**（750 行 / 37 天连续账本），但它以**完全绕开 melquiades 设计**的方式解决。这不是"工具坏了"，是**设计前提被证伪**。

---

## 2. 定位重定义

### 2.1 新定位

```
草稿纸 = 对话产出的账本（我写进去的、短的、密的、有序的、可改的）
知识库 = 外部语料的语料库（导入的书/笔记/对话、长的、大的、只读的）
```

| 维度 | 草稿纸（本项目） | 知识库（memoria-del-hielo） |
|---|---|---|
| 语料来源 | 对话产出 | 外部导入 |
| 规模 | 827 行 | 161,627 chunks |
| 可写性 | **可改**（追加式修订） | 只读（导入后不动） |
| 顺序性 | **强**（id 游标） | 弱（语义检索） |
| 消费者 | 当前对话的 LLM | 任意查询 |

**边界**：草稿纸不存对话正文；知识库不存会话账本。**两个库不合并。**

### 2.2 五维寻址（取代会话隔离）

| 维度 | 用途 | 现状 |
|---|---|---|
| `id` | 顺序浏览、增量游标 | 已有（PRIMARY） |
| `created_at` | 时间范围、分场追溯 | 已有（替代 session_id 做"哪一场"） |
| `tags` | 主题过滤 | **列已有，但 0 次使用**——见 §3.3 |
| `note_key` | 话题槽（可 UPSERT 覆盖） | 已有（UNIQUE） |
| `content` | 字面 / 语义检索 | 已有（双 FULLTEXT） |

**决策**：`session_id` **保留列，但重新语义化为 `bucket`（主题桶）**，默认值 **`default`**，检索默认跨桶。
- 理由：零迁移（列不动、老数据不动）；需追溯时仍可查（如"`appendixC` 那批是谁写的"）；与知识库的 bucket/tag 概念对齐
- **不删列**：删列不可逆，而保留的成本为零

---

## 3. 模块一 · 草稿纸（账本）

### 3.1 工具集：从 8 个砍到 5 个

只补裸 SQL 补不上的能力；**不重造已有的**。

| 工具 | 替代掉的裸 SQL 样板 | 说明 |
|---|---|---|
| `draftpad_write(content, tags?, key?)` | `INSERT ... (session_id,note_key,content,tags,model_name) VALUES (%s,…)`（实测有 **5 种列清单变体**） | 写入；`key` 给定时走 UPSERT（**话题槽**） |
| `draftpad_scan(from_id?, width?, limit?)` | `SELECT id, LEFT(content,400) ... WHERE ... deleted_at IS NULL AND id >= N ORDER BY id` | 顺序/增量读；默认过滤软删 + 前缀截断 |
| `draftpad_amend(ids, text)` | `UPDATE ... SET content=CONCAT(content,'\n\n────\n【…】') WHERE id IN (…)` | 批量追加修订；**事务包裹** |
| `draftpad_find(query, mode?, condition?)` | `WHERE content LIKE '%xx%'`（169 次） | 默认 FULLTEXT+向量；**`condition` 原样透传**给数值探针（见 §5.3） |
| `draftpad_tags()` | 无（所以沉睡） | 枚举 tags —— **让 `tags` 列活过来** |

**明确不做**：进程级 UUID 隔离、随机 note_key、`switch_session`、`get_session_id`、审计表（`draftpad_audit`）、`draftpad_vectors`（空表，见 §5.4）。

### 3.2 写入纪律（保留旧设计的好东西）

- **写操作不接收 session/bucket 参数**（沿用旧契约）——LLM 物理上不能写错桶
- 旧编号/旧桶**不设只读墙**（这是旧设计的病根：只读 = 主动遗弃记忆）
- `content` 截断上限进配置

### 3.3 让 `tags` 活过来

**观测**：LLM 每次写入都认真填语义 tags（`["口径修正","复算","焦面关系","话题12"]`），但 **tags 过滤 0 次**。
**推断原因**：裸 SQL 无法枚举"我到底填过哪些 tag"，猜不准就不敢用。
**对策**：`draftpad_tags()` 枚举 + `find(tags=[...])` 过滤。**零额外成本，纯收益。**

---

## 4. 模块二 · 压缩档案（DSH 压缩收割）

### 4.1 目标

把 DSH 上下文压缩产生的摘要**收割入库**，使：
1. 被压掉的内容**可被检索**（尤其"之前说过 XX 话题"这类模糊查找）
2. 检索到摘要后能**沿指针取回原文**（摘要 ≠ 真相）
3. 顺带产出 **context rot 曲线**（哪个会话/阶段压缩最频繁）

### 4.2 只收割，不接管（关键决策）

| 方案 | 做法 | 风险 | 选择 |
|---|---|---|---|
| 接管 | 写 TS 插件挂 `agent/pre-step`，压缩前拦截 | 进 DSH 进程；`duplicate loader entry id` 死机风险；preset 改动 | ❌ |
| **收割** | 离线读取 jsonl 的 `compaction/*` 事件 | 零风险（DSH 侧一行不动） | ✅ |

**依据**：
- 社区最强实现 `dsh-auto-memory` **也是扫 jsonl 找 `compaction/start`、`compaction/summary` 事件**，而非进程内回调
- 它自己的立场：「**不替宿主决定压缩——窗口是宿主的领地，她只负责交接**」
- 本项目的暴力检索器已证明离线扫描可行（50MB / 0.9s）
- **官方压缩 hook 点在 `agent/pre-step`，那是压缩前的时机**；我们要的是压缩后的产物，不需要那一刻

### 4.3 数据源（实测）

**位置**：`$DSH_HOME/sessions/<workspace>/session-<uuid>/session.v3.jsonl.zstd`

| 实测项 | 结果 |
|---|---|
| 规模（本机） | **71 个 zstd 日志 / 170 MB** |
| 单文件（当前会话） | 压缩 773,775 B → 解压 2,485,759 B，**180 个 zstd 帧** |
| 写入模式 | **多帧 zstd 追加流**（三次连续 stat：754,149 → 762,318 → 773,775 B，持续增长） |
| 活跃写入时可否读 | ✅ `stream_reader` 成功读出 343 个事件 |
| 压缩事件名 | `compaction/start`、`compaction/summary`（另有 `compaction/prune`） |

**注意**：`$DSH_HOME/sessions/` 在 **WSL 原生 ext4**（不是 drvfs）⇒ **inotify 可用**（备选触发方式）。
**另注意**：`D:\...\AI实用对话集\`下的 348MB/28 文件是**人工导出的副本**（其中 1033 个压缩点），与 live 目录是**两份数据**。**收割器盯 live 目录**，导出副本只作补充（避免小巴别塔）。

### 4.4 三触发（已定案）

| 触发 | 形态 | 说明 |
|---|---|---|
| **A. 定时器** | MCP 进程内 **daemon 线程** + `threading.Event` 停止信号 | 间隔 **60s**（配置项）。stdio MCP 进程寿命 = 会话寿命，**"会话期间常驻"恰好覆盖压点产生期** |
| **B. 搭车** | 工具调用时 **fire-and-forget** | 触发即返回，**不阻塞工具响应** |
| **C. 启动扫** | MCP 被加载时自动扫一轮 | 补齐上次会话结束后积累的压缩点 |

共用同一持久化游标 ⇒ 三条触发**互相幂等**。

**⚠️ 启动扫必须区分两种情况（否则会话第一秒就卡住）**：

| 情况 | 数据量 | 处置 |
|---|---|---|
| **首次全量扫**（游标表为空） | 170MB / 71 文件 | **不进启动路径**——用 CLI 手动预跑（`harvest.py --backfill`），或后台限速慢慢跑 |
| **启动增量扫**（游标已建立） | 仅新帧，通常 < 数百 KB | 可放启动，但仍走**后台线程**，不阻塞 MCP `initialize` 响应 |

**额外收益（本方案独有）**：本会话的压缩点会被本会话的定时器收割 ⇒ **"刚被压掉的内容"在同一会话内就可检索回来**。auto-memory 做不到（它选择压缩前写账本）。

### 4.5 增量读取与并发安全

**增量**：多帧 zstd + **字节偏移游标**（纯追加 ⇒ offset 足够，无需行指纹）。
> 指纹游标（`jsonl-tail-cursor` 的做法）是为应对"保尾丢弃 / 环形重写"文件的；DSH 会话日志是纯追加，不适用。可作为 offset 对不齐时的保险。

**并发**（已实测，8 线程 × 100 次 = 800 次并发写同一行）：

| 方案 | ok | 死锁 | 终值 | 耗时 |
|---|---|---|---|---|
| `INSERT ... ON DUPLICATE KEY UPDATE` | 800 | 无 | 800 ✓ | 0.15s |
| `INSERT IGNORE` + `UPDATE` | 800 | 无 | 800 ✓ | 0.26s |
| **纯 `UPDATE`（预 seed）** | 800 | 无 | 800 ✓ | **0.14s** |
| 条件 CAS `UPDATE ... WHERE val < ?` | 800 | 无 | 700 ✓（单调） | 0.14s |

**结论**：实测三者都能跑通，但 `INSERT ... ON DUPLICATE KEY UPDATE` 有**已知死锁倾向**（[MDEV-17073](https://jira.mariadb.org/browse/MDEV-17073)、MDEV-17603）——机制是**重复键时设共享锁 → S 锁升级 X 锁 → 死锁**（本实验的单行 PK 场景太简单，够不到触发条件，故**不得**据此判定它安全）。

**采用写法（原理上无锁升级路径）**：

```sql
-- seed（文件首次出现时，幂等）
INSERT IGNORE INTO dsh_harvest_cursor (file_path, offset, last_seq) VALUES (?, 0, 0);

-- 推进（单一 X 行锁，原子 CAS）
UPDATE dsh_harvest_cursor
   SET offset = ?, last_seq = ?, updated_at = NOW(6)
 WHERE file_path = ? AND offset < ?;
-- affected_rows = 1 → 我赢了；= 0 → 别人已推进 → 幂等跳过
```

`WHERE offset < ?` 的单调性已实测验证（游标只增不减）。

### 4.6 三层入库（防"只抄摘要必漏"）

**教训来源**：定案记录载明——摘要会把超时命令的部分输出记成"确认结果"**并跨会话传播**；且实测压缩比约 **35:1**（719 条消息 / ~426k tokens → 12K 摘要），**35 分之 1 的信息不可能保住所有检索词**。

| 层 | 内容 | 作用 |
|---|---|---|
| **L1 摘要** | `compaction/summary` 全文 + 元数据（session/turn/时间/遮蔽条数/token 数） | 检索入口（"去哪个压缩点找"） |
| **L2 指针** | 遮蔽 seq 列表 + 会话文件路径 + 帧偏移 | **回查路径**（唯一真相源定位） |
| **L3 防漏网** | 被遮蔽区的关键词集 / 首段 | 摘要没写、原文有的词 |

**定位钉死（不可漂移）**：

> 收割摘要 = **回查路径的入口索引**
> **不是** 摘要替代原文

**依据**：文献与实践调研结论——「**所有"做对了"的方案赢在加【回查路径】而非写更好的摘要**」，且「**文献里没人验证过"事后捞摘要"**」。守住"入口索引"定位，即落在已被验证的赢家模式内。

**摘要展示必须标注**「有损、可能失真」（沿用既有定案）。

### 4.7 运行日志表（DZ 老登方案）

```sql
dsh_harvest_log(
  run_id, started_at, finished_at, status(ok|partial|error),
  trigger(timer|tool|manual), files_seen, files_scanned,
  frames_read, events_new, compactions_new, cursor_advanced,
  error_text
)
```

**两个作用**：
1. 不静默降级（铁律四）——失败可查
2. **context rot 曲线的数据源**——`GROUP BY session/turn` 直接得出"哪个会话压缩最频繁"

---

## 5. 模块三 · 检索栈（两模块共用）

### 5.1 两路都要（学术与工业双重共识）

| 证据 | 内容 |
|---|---|
| Blended RAG（arXiv:2404.07220） | dense + sparse + hybrid query 在 NQ/TREC-COVID/SQuAD 全面更优 |
| DESA（arXiv:2608.15851） | **"Dense expands, Sparse anchors"**——两路应**协同设计**；BEIR nDCG@10 +3.82% |
| Mem0（工业界） | retrieve 阶段 **fuses parallel scoring passes（semantic + keyword + entity）** |
| dsh-auto-memory | 同为**三臂融合**：词法 + 语义 + 时间 |
| dsh-memoir | 纯 BM25、零 embedding —— 说明**词法仍是一等公民** |

### 5.2 检索实证（来自真实调用）

| SELECT 用法 | 次数 | 占 517 条 SELECT |
|---|---|---|
| `id >= N` 游标扫 + `LEFT(content,N)` | ~338 | 65% |
| **`LIKE` 模糊查找** | **169** | **33%** |
| `MATCH()` 全文 | **0** | — |
| `tags` 过滤 | **0** | — |

**LIKE 模式的性质分解**（610 个模式串）：

| 类型 | 个数 | 占比 | 由谁服务 |
|---|---|---|---|
| **纯中文主题词**（`'%口径%'`） | **382** | **63%** | ✅ FULLTEXT / 向量明显更优 |
| 纯英文/符号 | 148 | 24% | ✅ FULLTEXT |
| 中英混（术语/口径名） | 51 | 8% | ⚠️ 向量更稳 |
| **纯数值探针**（`'%19.7463%'`） | **29** | **5%** | ❌ **FULLTEXT/向量都替代不了，必须留 LIKE** |

### 5.3 保留 LIKE 通道的理由（重要反例）

```sql
SELECT id, (content LIKE '%19.7463%') AS old_M,
           (content LIKE '%19.7413%') AS new_M,
           (content LIKE '%39.4926%') AS old_Rs, ...
FROM draftpad WHERE ...
```

这是**布尔矩阵审计**——一次查询给出"哪条含旧口径值、哪条已改新值"。FULLTEXT/向量给的是**排序结果**，给不了逐值真值表。实测中**单条语句最多塞了 14 个 LIKE**（含 ≥3 个 LIKE 的约 84 条）。

⇒ `draftpad_find` 必须保留 `condition` **原样透传**能力。工具的检索封装**不得剥夺 LIKE 的表达力**。

### 5.4 向量落地（已定：**主表加列**）

**决定**：在 `draftpad` 上加 `vector(1024)` 列 + VECTOR INDEX（**不用**空表 `draftpad_vectors`）。
`dsh_compaction` 同样加一列（摘要的语义检索）。

```sql
-- 加列 + 建向量索引（DISTANCE / M 必须显式写）
ALTER TABLE draftpad ADD COLUMN embedding VECTOR(1024) NULL;
ALTER TABLE draftpad ADD VECTOR INDEX idx_vec_draftpad (embedding) M=16 DISTANCE=cosine;
```

**三条 MariaDB 实测坑（memoria-del-hielo 踩过，必须遵守）**：

| 坑 | 说明 |
|---|---|
| 省略 `M` / `DISTANCE` | **静默降级**为 `M=6 + euclidean`——不报错，只是精度变了。**必须显式写** |
| 删除向量索引 | `ALTER TABLE ... DROP VECTOR INDEX` → **ERROR 1064**；正确写法是 `DROP INDEX idx_name` |
| **每表仅一个 VECTOR INDEX** | MariaDB 硬限制：一张表只能有一个向量索引 ⇒ 维度方案必须**一次定死**，换模型要 `ALTER` 列 |

- `draftpad_vectors`（现 0 行）：**保留空表、不再使用**（删表不可逆，留着零成本）
- 嵌入后端：本地 `server_bge`（9005，BGE-M3 / 1024 维）
- 中文检索：**MariaDB 没有 `WITH PARSER ngram`（那是 MySQL）** ⇒ 依赖 **jieba 预分词写入 `content_tokenized`**（`draftpad` 已有此列 + `ft_draftpad_tokenized` 索引）

### 5.5 多路检索与兜底链（已定）

**检索不是一条路，是多路 + 显式兜底。** `find(query, mode?, …)` 支持五种，按序降级：

| 路 | mode | 机制 | 适用场景 |
|---|---|---|---|
| ① **融合** | `auto`（默认） | 向量 + FULLTEXT 加权并置 | 常规模糊查找（"之前说过 XX 话题"） |
| ② **向量** | `vector` | BGE 余弦相似 | 语义相近但字面不同 |
| ③ **全文** | `fulltext` | FULLTEXT BOOLEAN（jieba 分词） | 有明确关键词、要多词布尔 |
| ④ **关键词** | `keyword` | **LIKE 串联**（多词 OR / AND） | 精确字面、写法/大小写不定 |
| ⑤ **探针** | `condition` | 原样 SQL 片段透传 | 数值布尔矩阵审计（§5.3） |

**兜底链**（`auto` 模式）：

```
向量 + FULLTEXT 融合
      ↓ 空结果？
LIKE 串联（宽松 OR，逐词放宽）
      ↓ 仍空？
显式报告"未命中" + 标注走到哪一步   ← 绝不静默空手而归
```

**三条依据**：
1. **文献**：Blended RAG / DESA「两路协同」；Mem0「fused parallel scoring（semantic + keyword + entity）」
2. **实测**：169 次 LIKE 中 **84 条含 ≥3 个 LIKE**（多关键词串联是真实高频用法，最多一条 14 个）
3. **既有实践**：memoria 的 `fulltext_only` 空结果自动降级 + `search_meta.fallback` 显式标注——"不能据此断定不存在"

> **返回结构**约定：结果列表 + `meta`（用了哪条路、是否降级、各路口径）。降级必须**可观测**，否则就是静默降级（违反铁律四）。

---

## 6. 铁律与纪律（实施细则）

### 铁律一 · 即弃连接

**规则**：MariaDB 连接**用完即关**，不使用长连接/连接池，除非有明确理由并记录在案。

**理由**（血教训）：长连接出错后**捞不回来**，且会**占满连接池**。

**⚠️ 与现状的冲突（必须处置）**：当前 `config.yaml` 有
```yaml
pool_name: "melquiades_pool"
pool_size: 5
```
而 `db.py` 使用连接池 —— **与铁律一冲突**。

**决定（已拍板）：改** —— `db.py` 与 `config.yaml` 均去掉连接池，改为即弃连接（每次操作 `connect → execute → close`）。若个别路径确需池化，须**显式列出理由并记录**。

### 铁律二 · 复用靠 copy，不靠 import

**规则**：从 `memoria-del-hielo` 等项目复用代码时，**copy 到本项目**，不用跨项目 import。

**已知待 copy 清单**：
| 来源 | 目标 | 说明 |
|---|---|---|
| `memoria-del-hielo/src/core/tokenizer.py` | 本项目 | jieba 分词 + CJK 检测 + `tokenize/tokenize_query` |
| `memoria-del-hielo` 的 `search.py` 双路合并/降级逻辑 | 本项目 | 仅参考，按本项目权重重写 |
| `dsh-auto-memory/lib/handoff-anchor.js` 的**四段权重截断**思路 | 本项目 | 用于预算不足时的展示截断（见下） |

**copy 时的纪律**：文件头注明来源项目、来源路径、取值日期（便于日后核对漂移）。

### 铁律三 · 魔法数字进配置

必须进配置的项（示例）：

```yaml
draftpad:
  content_max_chars: ...        # 写入截断上限
  scan_default_width: 400       # LEFT() 截断宽度
  scan_default_limit: ...

dsh:
  home: /home/<user>/deepseek-harness/dsh-domus   # 显式，不猜
  sessions_dir: sessions
  scan_interval_sec: 60
  first_scan_budget_files: 10   # 首扫限速
  first_scan_frames_per_file: ... 
  harvest_enabled: true

search:
  fts_weight: ...
  vector_weight: ...
  top_k: ...
```

### 铁律四 · 不静默降级

- 收割失败 → 写 `dsh_harvest_log`（不吞）
- 单文件损坏（zstd 半帧 / 权限）→ 记录 + **跳过继续**（不影响其他文件）
- **收割器的任何异常都不得使草稿纸工具不可用**（参考 auto-memory P15 教训：`apply` 抛错导致整棵插件树崩 `AggregateError`）

### 补充纪律

| 纪律 | 说明 |
|---|---|
| **可急停** | 收割线程带 `threading.Event` 停止信号，循环内每轮检查 |
| **DSH_HOME 解析唯一口径** | 逐级回落：① 显式配置 → ② `env DSH_HOME` → ③ `os.homedir()/.dsh` → ④ `USERPROFILE\|\|HOME/.dsh` → ⑤ 保证非空。**只读不缓存、永不抛**（抄 auto-memory `dsh-home.js` 的纪律；它记录了"7 处独立解析导致数据写到不同根目录"的事故） |
| **线程与 GIL** | zstd 解压与 DB I/O 均释放 GIL，故 `threading` 真并行（等价于 C 的 `_beginthreadex` 方案） |
| **preview → 确认** | 文件写入遵循"先读后改"，行号定位走"预览→确认"两阶段 |

---

## 7. 老数据处置

| 数据 | 现状 | 建议 |
|---|---|---|
| `draftpad` 827 行 / 21 个 session_id | 真实生产账本（750 行是 `bdd1982f-ice-planet`） | **不动**。`session_id` 保留列、重定义为 bucket；`created_at` 承担"哪一场" |
| `draftpad_audit` 0 行 | 从未生效 | **保留空表**（删表不可逆，留着零成本）；新设计不依赖它 |
| `draftpad_vectors` 0 行 | 从未生效 | 启用为向量存储（见 §5.4），或按 §5.4 备选方案 |
| `scratch_notes` 9 行 | 极简表（`id/ts/topic/note`），内容实为**状态锚点/checkpoint**（dsh迁移锚点、ice-planet 恢复点）与附注，**非生产表**；与 `draftpad` 配合使用（后者存正文账本） | **已定：保留原位，仅改名以标明非生产性质**（建议 `misc_notes`）。**不迁移、不删除** |
| 6 条软删（`deleted_at` 非空） | 正常 | 不动 |
| `D:\...\AI实用对话集\` 348MB / 1033 压缩点 | 人工导出副本 | **不作为收割源**（避免与 live 目录形成两份真相）；仅作历史补录的备选 |

---

## 8. 与既有系统的边界

| 系统 | 关系 |
|---|---|
| `memoria-del-hielo`（知识库） | **不合并**。草稿纸=对话产出/短/可改；知识库=外部语料/长/只读。代码复用靠 **copy**（铁律二） |
| `mcp_session_recall`（暴力检索） | **本方案落地后它可退役**——其三项能力（字面检索 / 按 seq 取原文 / 压缩点清单）均由新模块接替。退役需逐项确认无遗漏 |
| `dsh-session-insights` | 不冲突。它做"行为复盘"（一次性报告），我们做"记忆索引"（持久库） |
| DSH 自身 | **零侵入**（只读它的日志文件） |

---

## 9. 分期实施建议

| 期 | 内容 | 风险 |
|---|---|---|
| **一期** | 收割器（双触发 + 偏移游标 + 三层入库 + 日志表） + 检索栈（FULLTEXT+向量） | 低（进程外、只读源、新增表） |
| **二期** | 草稿纸 5 工具 + `session_id` → bucket 语义切换 | 中（涉及旧工具下架） |
| **三期** | context rot 曲线 + 用数据反哺 System Prompt | 低（纯分析） |
| **最后** | 旧 `README.md` 的重写（描述旧 8 工具设计，需与新设计对齐）——**已定：放到落实代码时一并改** | — |
| **待定** | 是否按一期先行（用户考虑中） | — |

---

## 附录 A · 字段契约

### A.1 `draftpad`（沿用，必要时增列）

```
id            INT AI PK            顺序/游标
session_id    VARCHAR(36)          → bucket（主题桶），默认单一值
note_key      VARCHAR(128)         话题槽（UNIQUE(session_id,note_key)）
content       TEXT/LONGTEXT        正文
content_tokenized TEXT              jieba 分词结果（CJK 检索用）
tags          VARCHAR(1024)        JSON 数组字符串
model_name    VARCHAR(100)
chunk_index   INT
created_at    TIMESTAMP(6)         时间维度（承担"哪一场"）
updated_at    TIMESTAMP(6)
deleted_at    TIMESTAMP(6) NULL    软删
embedding     VECTOR(1024) NULL    向量（**新增**，见 §5.4）
```

索引：`PRIMARY(id)`、`idx_draftpad_key UNIQUE(session_id,note_key)`、`idx_draftpad_session`、`idx_draftpad_deleted`、`ft_draftpad_content`、`ft_draftpad_tokenized`

### A.2 `dsh_harvest_cursor`

```
file_path     VARCHAR(512) PK     会话日志文件绝对路径
offset        BIGINT              已消耗字节偏移（多帧 zstd 边界）
last_seq      INT                 最后处理的 seq（去重保险）
updated_at    TIMESTAMP(6)
```

### A.3 `dsh_compaction`（收割产物）

```
id            INT AI PK
dsh_session   VARCHAR(64)          DSH 会话 uuid
file_path     VARCHAR(512)
turn_no       INT
compaction_id VARCHAR(64)
shadowed_count INT                遮蔽条数
shadowed_seqs MEDIUMTEXT          遮蔽 seq 列表（L2 指针）
tokens_before BIGINT              遮蔽 token 数
summary       MEDIUMTEXT          L1 摘要全文（**标注有损**）
summary_tokens INT
keywords      TEXT                L3 防漏网层
occurred_at   TIMESTAMP(6)
embedding     VECTOR(1024) NULL     摘要向量（**新增**，见 §5.4）
```

### A.4 `dsh_harvest_log`

见 §4.7。

---

## 附录 B · 配置项清单

见 §6 铁律三示例。**所有魔法数字必须出现在此**，代码中只引用常量/配置。

---

## 附录 C · 本次调研的实测数据索引

| 数据 | 值 | 用途 |
|---|---|---|
| `draftpad` 行数 | 827 | 规模 |
| `bdd1982f-ice-planet` 行数/跨度 | 750 行 / 2026-08-16 → 09-22 | 跨对话账本证据 |
| 随机 note_key 条数 | 19（仅 2026-07） | 旧设计唯一一次生效 |
| DSH 中 mariadb 工具调用 | 984 次（59:492 / 60:334 / 61:159） | 绕过证据 |
| `MATCH()` / 向量 调用 | 0 / 0 | 检索闲置 |
| `LIKE` 调用 | 169（含 29 数值探针） | 检索需求 |
| 单条 SQL 最多 LIKE | 14 个 | 布尔矩阵审计 |
| DSH 会话日志 | 71 文件 / 170MB / 多帧 zstd | 收割源 |
| 单会话文件 | 773,775 B 压缩 → 2,485,759 B 解压 / **180 帧** | 增量方案 |
| 历史压缩点（导出副本） | 1033 个 / 3.1MB | 规模估计 |
| MariaDB 并发实测 | 800 次 × 4 方案 | 并发选型 |
| 压缩比 | ~35:1（719 条 / 426k tokens → 12K 摘要） | 三层入库的理由 |

---

## 附录 D · 参考文献与业界对照

### D.1 学术

| 文献 | 要点 |
|---|---|
| Nye et al. 2021, arXiv:2112.00114 *Show Your Work: Scratchpads for Intermediate Computation* | **"scratchpad" 的学术祖源** |
| arXiv:2602.06052 *A Survey of Agent Memory in the Second Half* | 2025 数百篇；memory substrate / cognitive mechanism / memory subject 三维度 |
| arXiv:2601.01885 *Agentic Memory (AgeMem)* | **把 memory operations 暴露为 tool actions**（store/retrieve/update/summarize/discard） |
| LongMemEval（ICLR 2025） | 500 题，含 multi-session reasoning / temporal / **knowledge updates**；**持续交互约 30% 准确率下降** |
| arXiv:2607.13157 *Oracle Agent Memory* | 数据库原生记忆基底；**lifecycle 含 revision/removal**；**比 flat-history 省 10.7× tokens**；LongMemEval 93.8% |
| arXiv:2404.07220 *Blended RAG* | dense + sparse + hybrid 全面更优 |
| arXiv:2608.15851 *DESA* | "Dense expands, Sparse anchors"；BEIR nDCG@10 +3.82% |
| arXiv:1811.08772 *CAR / low-utility facets* | "某些检索面向几乎不会逐字出现在答案中"——LIKE 字面匹配失效的学术命名 |
| arXiv:2502.00158 *LOKA* | 知识"编辑 vs 遗忘"的任务冲突 |
| arXiv:2606.24322 | 记忆投毒；**"agent 自己的摘要"是洗白渠道之一** ⇒ 摘要必须标注有损 |
| Zep | 时效知识图（事实带有效期）——与"口径·日期 + 指针"同构 |

### D.2 业界插件对照（5 个 DSH 记忆插件）

| 插件 | 存储 | 检索 | 注入 | 可借鉴 |
|---|---|---|---|---|
| **dsh-auto-memory** | 文件系统 4 层 | 三层下探（Tier-0 目录 800 token → Tier-1 摘要 140 字符 × 8 → Tier-2 原文 ≤2400）+ 三臂融合 | Host 侧联想中间件（不靠模型调工具） | **扫 jsonl 的 `compaction/*`**；`handoff-anchor` 四段权重截断；`jsonl-tail-cursor` 指纹游标 |
| **dsh-memories** | `<scope>.json` **scope=项目名** | 子串 | 无 | 极简 KV；**作用域是项目不是会话** |
| **dsh-mnemon** | 三层 + **9 个 Provider** | 按需召回（`guided`） | 每回合一个上下文视图 | `storage-lock`（并发锁是公认问题）；embedding 默认关 |
| **dsh-memoir** | JSON SSOT + `PROJECT_MEMORY.md` | **纯 BM25** | 有界 Hot Memory（token 硬预算） | "无向量"路线可行；`turn-end` 触发 |
| **dsh-session-insights** | 不存 | — | — | 用官方 `sessionQuery` 读会话（备选路径） |

**行业方向印证**：2026-09 **GPT-6 Astra** 把「上下文管理」作为旗舰实验特性（笔记跨窗口保留、历史归档可搜索、**倾向交接而不是压缩**）。

### D.3 插件接入契约（若将来需要进进程，非本稿方案）

```
挂钩点（waterfall）：
  tools/post-execute | tools/pre-execute | approval/request
  llm/stream         | agent/turn-stopping
静态包写法（link 安装）：export default { name, inject:['tools'], apply(ctx){ ctx.tools.register(def) } }
  ⚠️ 全局 harness 仅存在于动态插件沙箱；静态包引用它会启动即崩（AggregateError）
  ⚠️ apply 必须 try/catch 永不抛，否则整棵插件树崩
  ⚠️ duplicate loader entry id → 死机（同一批 id 只允许一层定义）
```

---

## 已拍板 / 待定

### 已拍板（2026-09-22）

| # | 问题 | 决定 |
|---|---|---|
| 1 | 铁律一与现状冲突（连接池） | **改** —— 去池化，即弃连接 |
| 2 | 旧 `README.md` 重写 | **放到落实代码时一并改** |
| 3 | 向量落地方式 | **主表加列**（`draftpad` + `dsh_compaction` 各加 `vector(1024)`） |
| 4 | `session_id` → bucket 默认值 | **`default`** |
| 5 | 收割触发 | **定时器 60s** + **搭车** + **MCP 加载时自动扫一轮** |
| 6 | `scratch_notes` 处置 | **保留原位，改名标明非生产**（建议 `misc_notes`）；不迁移、不删除 |

### 待定（用户考虑中）

| # | 问题 |
|---|---|
| 7 | 是否按 §9 的一期先行（收割器 + 检索栈） |
