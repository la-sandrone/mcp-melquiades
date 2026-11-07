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

> **后续（2026-10-30）**：`dbcore.py` 的即弃连接落地后，`db.py` 与只消费它的 `search.py`
> 已成死代码（全仓无 import、无动态加载），已整体删除。本决策不再有"遗留"尾巴。

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
  home: <harness-dir>/dsh-domus   # 显式，不猜
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

### 已拍板（2026-11-07 · 会话正文（messages 源）的事件过滤）

> **状态：已决策，未实施。** 起因：侧边栏统一检索「打捞不出来」的排查（证据见本节表格与
> 2026-11-07 的 CHANGELOG 条目，实施时补）。

| # | 问题 | 决定 | 依据（实测） |
|---|---|---|---|
| 8 | 同一条消息被 `agent/inbox/spliced` 与 `user/message` 各记一遍 | **两者都收割**（不漏信息）；**默认只查 `user/message`**；查 splice 须显式加参数 | 同一 `UserMessage.id` 落在两条事件上：前者是控制面（inbox 队列变更，载荷 `inserted: UserMessage[]`），后者是内容面（对话表面，载荷即 `UserMessage`）。全库 12,220 条 splice 行中 12,208（**99.9%**）与同会话 `user/message` 正文逐字相同；仅 12 条孤儿（background job / approval policy 通知 + 1 条被取消的用户消息）。**不是 bug，是设计**——`outcome:'canceled'` 的 splice 只有队列事件 |
| 9 | messages 源怎么决定「什么算会话正文」 | **白名单**，不是黑名单；**写进 `config.yaml`** | 当前无任何过滤，于是 `system/message` 394 行（单条最长 **44,883 字**＝整篇系统提示词）、`web/deepseek-search-llm-request` 1,700 行、`session/title-llm-request` 161 行、`compaction/summary` 244 行（与 `compactions` 源同文本、抢两个名额）全都在「会话正文」里参与排序 |
| 10 | 查 splice 的参数形态 | **三态** `off`(默认) / `only` / `both`，**不是布尔** | 「需要查另一个」隐含「**只**查另一个」——用于捞「改口前的原话」（实测样本：caaa597c seq=614，被取消的那版只存在于 splice） |

#### 默认白名单（「对话表面」事件）

`user/message`、`assistant/message`、`tool/result`、`tool/call`、`tool/ptc-dispatch`

其余事件一律要参数才进。**为什么不用黑名单**：DSH 每次升级都可能新增控制面事件（本版
catalog 已 62 个类型），黑名单会让新类型**默认自己漏进来**，症状是沉默的、延迟发作的。

#### 升级适配工作项（每次 DSH 升级必做）

`packages/core/session/src/known-event-types.ts` 是 DSH 的**权威事件目录**（自动生成，
`pnpm run gen-persistence-catalog`，由 `doc-sync` 校验新鲜度）。

1. 取新版的 `KNOWN_SESSION_EVENT_TYPES`，与 `config.yaml` 里的白名单做**差集**；
2. 差集里凡「能抽出 `text`/`reasoning`/`content` 块」的新类型 → 必须显式判定进白名单还是排除；
3. 判定结果写回 config 注释，并补一条 CHANGELOG。

漏做这一步的后果：新事件类型静默进入「会话正文」检索，重现 2026-11-07 这类噪声污染。

#### 待实施清单（本决策派生）

- [ ] `config.yaml` 增 `search.messages_ev_types`（白名单）与 `inbox` 三态默认值
- [ ] `omnibus.py` 的 `SOURCE_SPECS['messages']['where']` 改为按白名单 + `inbox` 参数拼接
- [ ] 桥 `/search` 与 `search_OMNIA` 各加 `inbox` 参数，并回显生效值
- [ ] 升级适配：把「目录差集」脚本化（挂在 `melquiades status` 或独立 CLI）

---

### 已拍板（2026-11-07 · 查询侧：停止词表 + schema 教 LLM 怎么写查询）

> **状态：已决策，未实施。** 起因同上（NL 问句打捞不出来）。

| # | 问题 | 决定 | 依据（实测） |
|---|---|---|---|
| 11 | 自然语言问句被 AND-bigram 化 ⇒ 退化成「整串子串匹配」 | **停止词表**（查询侧，切块后**块内**生成 bigram）+ **schema 教 LLM 怎么写查询** + **返回侧诊断**，三层兜底 | 9 条 NL 查询实测：目标会话进前 50 的从 **3/9 → 7/9**。「等离子体生命算不算生命」原样查目标 0 命中 → 剥掉「算不算」后命中 48 条、目标排第 5 |

#### 三条配套铁律（缺一条就白做）

1. **必须切块，不能删字符**。`q.replace(算不算, 空)` 再 bigram 会造出新垃圾：
   「等离子体生命生命」→ `+命生`（跨边界）。要用占位符切开，**只在块内生成 bigram**。
2. **不要顺手把 AND 改 OR**。项目里那条漂移（`tokenize_query` 去 `+`，jieba 词口径，200/232→0/232）
   **不能平移到 bigram 口径**：实测停词+AND 命中 48、目标第 5；停词+OR 命中 **5,984**、且
   「找这段话的上下文讨论」排名从第 2 掉到第 39。词 token 少而准，bigram 多而冗余。
3. **停止词表里不放指代词**。「那个/这段/这种」区分度低，删掉反而让噪声行挤进前排
   （实测「找这段话的上下文讨论」排名 2 → 10）。该放的是**疑问框架词 + 虚词 + 介词连词**
   （第一版漏了「关于/只要/哪怕」，残留 `于等`、`子只` 这类边界垃圾）。

#### 收口点与配置

- 改 **`build_cjk_boolean` 一处**即可：它只被查询侧调用（`omnibus.py:195`、`retrieval.py:622`），
  索引侧走 `tokenize_cjk_ub`。一处同时修好「会话正文」与「草稿纸/压缩摘要」的全文路。
- **绝不碰索引侧**：`body_tokenized` 是 unigram+bigram 全量，索引侧一过滤，单字与生造词就废了
  ——正好毁掉选 bigram 的理由（Lucene CJKBigramFilter / ES cjk_bigram / SIGIR'98：不依赖词典）。
- 停止词表进 `config.yaml`（铁律三）。
- 过滤后块为空 ⇒ **用原查询**，绝不因过滤返回 0（不静默降级）。

#### schema 要改两处（含一条**半错**的既有建议）

- `search_OMNIA`：描述里**一个字都没讲查询该怎么写**（参数/边界/HNSW 纪律都写了）。
  补「查询写法」段：用关键词不用问句；**一个主题一次调用**；关键词 2–4 字；
  找原话时反过来整句贴（LIKE 才是精确那条）；放宽顺序。
- `draftpad_find`：现有「**多线索用空格分隔**」对 clue 路正确、对 bigram 全文路**必死**
  （4 块 = 4 组 bigram 全 AND）。改为**按 mode 分治**。

#### 返回侧诊断（0 命中时不许只说「未命中。」）

信号已在手（停止词过滤时顺带产出）：分块数 > 1、被剥掉的停止词非空、query 长度 > 12。
命中即输出「为什么 0 命中 + 下一步怎么做」，让 LLM 能自纠而不是瞎换词重试。

#### 明确不做

多主题并列的 **k-of-n / IDF 选块必中**：实测「最长块必中 + 其余只打分」无增益（7/9 不变）。
上游是 LLM，**拆查询的成本近乎零**——在 schema 里教它拆，比在 SQL 层替它猜便宜得多。

#### 待实施清单（本决策派生）

- [ ] `config.yaml` 增 `search.stopwords`（含「不放指代词」的注释说明）
- [ ] `tokenizer.build_cjk_boolean` 加切块 + 块内 bigram + 空块兜底
- [ ] `server.py` 改 `search_OMNIA` / `draftpad_find` 描述（按 mode 分治）
- [ ] `omnibus.format_hits` 的 `if not hits` 分支输出诊断 + 下一步
- [ ] 修 `search_OMNIA` 描述里的过期事实（见下条 #12）

---

### 已拍板（2026-11-07 · 会话正文的向量路：补 VECTOR 索引）

> **状态：已决策，未实施；用户明确「一定要做」。** 理由：本项目所有使用场景严重依赖上下文，
> 甚至跨对话上下文——语义召回不覆盖会话正文，等于把最大的一块语料挡在语义检索之外。

| # | 问题 | 决定 | 事实 |
|---|---|---|---|
| 12 | 「会话正文没有向量路」的**理由写错了** | **补 VECTOR 索引 + 补齐嵌入**，并同步修正所有过期注释 | 列**存在**，已有 **33,993 行 embedded=1（16.5%）**（`tools/lane-a-embed-messages.py` 跑过一部分）；但 `SHOW INDEX FROM dsh_message` **没有任何 VECTOR 索引**（`draftpad` / `dsh_compaction` 都有）。无索引时 `VEC_DISTANCE_COSINE` 退化为**全表暴力扫 20.6 万行 × 1024 维**——这才是 `vec: False` 的真实原因，不是「没有列」 |

#### 要改的过期表述（三处，口径必须统一）

- `omnibus.py` `SOURCE_SPECS` 注释：「dsh_message 没有 embedding 列：补向量是 14 万行的大工程」
- `omnibus.py` `search()` 的 notes：「会话正文无向量列（未做 embedding）」
- `server.py` `search_OMNIA` 描述：「会话正文（dsh_message）**没有向量列**」

新口径：**列存在、已嵌 16.5%、缺 VECTOR 索引**（无索引即全表暴力扫 20.6 万×1024 维）。

#### 工作量与顺序（待细化）

1. 建索引：`ALTER TABLE dsh_message ADD VECTOR INDEX idx_vec_message (embedding) M=16 DISTANCE=cosine`
   —— 按 README 的性能纪律，MariaDB 上这类 DDL **按小时算**，且别与重 IO 任务并行。
2. 补嵌剩余 ~17.2 万行（`tools/lane-a-embed-messages.py`，断点靠 `embedded=0`）。
3. 打开 `SOURCE_SPECS[messages][vec]`，并给向量路加 `embedded = 1` 过滤（零向量行会拿假最高分）。
4. 回归：HNSW 是近似检索，按既有纪律只用于语义召回，**不可判重/自查询**。

#### 待实施清单

- [ ] 建 `idx_vec_message`（M=16 DISTANCE=cosine）
- [ ] 补嵌剩余行，核对 `embedded=1` 覆盖率
- [ ] 开 messages 向量路 + 零向量过滤
- [ ] 三处过期表述统一改口径
- [ ] 回归 top-10 recall（参照 README 的 HNSW 实测方法）

---

### 已拍板（2026-11-07 · 排序与降级：工作区作用形态 / 静默降级 / 重复呈现）

> **状态：已决策，未实施。**

| # | 问题 | 决定 | 理由 |
|---|---|---|---|
| 15 | 工作区在排序里怎么起作用 | **三态**：`boost`（默认加权）/ `filter`（硬过滤，可选）/ `tiered`（分层展示） | 跨工作区找回是真实需求（本次排查就是从 melquiades 找冰质星球的东西），硬过滤会让这个能力消失 ⇒ 默认必须软 |
| 16 | 三路异常被吞成 0 命中（`ok:true, hits:[]`） | **修**：`stats.degraded_routes` 透到 UI，notes 说清哪一路死了、为什么；**DB 挂了要吵** | 铁律四「不静默降级」。现状 `_vec`/`_fulltext`/`_like` 各自 `except → return []`，桥还持一条长连接（与铁律一相悖），连接废掉的表现是「搜不到」而不是「桥挂了」 |
| 17 | 三路命中同一行时怎么呈现 | **只呈现一次 + 标明来源**（`matched_by` 徽章已具备）；**递补**：去重后不足 `limit` 条时，从各路更深排名拉上来补足 | 消费者是 LLM，看得懂来源徽章，不需要重复行；但去重不能让结果变少 |

#### #15 的前置：工作区映射从哪来（不要回填 20 万行 cwd）

| 数据 | 权威源 | 覆盖（实测） |
|---|---|---|
| 会话→工作区（标题+路径） | `storages/workspace.json` → `tables.workspaces[*] = {path,title,sessionIds}` | 19 个工作区 / **覆盖 86.2% 的行** |
| 会话标题 | `storages/session_projcache/sessions/*.json` → `record.rows.title.val` | 131/280 会话 |
| 行级 cwd | `dsh_message.cwd` | 只有 57%，且目标会话全 NULL（增量批次里没有 header 事件） |
| 会话级 cwd | `dsh_session.cwd` | 只有 63 行，且 4 行缺 `session-` 前缀 |

⇒ 建 **`dsh_session_meta(session_id, cwd, workspace_title, title, …)`**，在 **session 粒度** join，不回填行级 cwd。
⚠️ 注意格式不统一：`dsh_compaction.dsh_session` 存**裸 uuid**，`dsh_message.session_id` 带 `session-` 前缀，join 前必须归一。

#### #17 的口径（★ 与 2026-10-25 的拍板对齐，勿混为一谈）

- 10-25 拍的是「**跨路去重后全给、不截断回 limit**」= 不设**上限**。
- 本次加的是「**去重后不足 limit 条时，从更深排名递补**」= 设**下限**。
- 两条不冲突：**下限保证 ≥ limit 条不同记录，上限不设**。
- 实现上「递补」= **加深取数深度**（`per_route_n` 动态放大 / 不足则二次深取），不是截断。
- ⚠️ 待确认：若用户本意是「最终只输出 limit 条」，则与 10-25 冲突，需另行拍板。

#### 其他重复的归属（已在别处拍板，此处只做索引）

| 重复来源 | 处置 | 决策 |
|---|---|---|
| `agent/inbox/spliced` ↔ `user/message`（99.9% 成对） | 默认只查 `user/message`，splice 需参数 | #8 |
| `system/message` / `web/*-llm-request` / `session/title-llm-request` | 白名单排除 | #9 |
| `compaction/summary` 回灌成 `user/message` | 由白名单 + `compactions` 源承担 | #9 |
| 样板文本（如 1,136 次重复的 `(execute_sql returned no model-visible content)`） | 白名单挡不住，靠 #17 的呈现层去重 | #17 |

#### 待实施清单

- [ ] 建 `dsh_session_meta` + 收割 projcache / workspace.json（含 session_id 前缀归一）
- [ ] 桥与 `search_OMNIA` 加 `workspace` 三态参数，回显生效值
- [ ] `omnibus.search()` 三路异常写进 `stats.degraded_routes`，`format_hits` 与 UI 显示
- [ ] 递补逻辑：去重后 distinct < limit ⇒ 加深 `per_route_n` 二次取数
- [ ] `dsh_compaction.dsh_session` 与 `dsh_message.session_id` 的格式归一（防第二次「数据巴别塔」）

---

### 已拍板（2026-11-07 · **#11 修订**：全文路改走 NATURAL LANGUAGE MODE）

> **状态：已决策，未实施。本节取代 #11 里「自己拼 AND 布尔式」那一半；停止词表、块内切块、schema 教写法、返回侧诊断四条不变。**
> 为什么留这条修订记录：原方案（停词 + 块内 bigram + AND）只解决了「框架词制造垃圾 bigram」，没有解决「AND 语义本身毁召回」——而后者才是主因。

#### 五变体实测（12 条查询，目标＝冰质星球那两个会话）

| 变体 | 覆盖 | 中位排名 | 单次耗时 |
|---|---|---|---|
| A 现状（AND，unigram+bigram） | **3/8** NL 问句 | — | — |
| B 停词 + 块内 bigram + AND | 11/12 | ~7 | 数百 ms |
| C NL 模式 + 全 token | 7/8 NL | — | — |
| **D NL 模式 + 只 bigram + 停词** | **12/12** | **4.5** | **61 ms** |
| E NL 模式 + 只 bigram，无停词 | 7/8 NL | — | — |
| RRF(B, D) 融合 | 12/12 | 8.0 | — |
| D 为主 + B_top5 加成 | 12/12 | 7.0 | 120 ms |

关键对照：「尘埃等离子体 生命 判定」B=**无命中**、D=第 10；「等离子体生命算不算生命」B=第 8、D=**第 3**；「四支柱」B=第 7、D=**第 5**；短关键词（Lyfe/Bartlett/病毒）两路持平。

#### 新方案（比原方案更简单）

```
chunks = 停词切块(query)        # 块内 bigram；单字块退回 unigram；空则用原查询
expr   = " ".join(terms)        # ← 空格分隔，没有 + 号
SQL    = "… AGAINST(? IN NATURAL LANGUAGE MODE) ORDER BY 分数 DESC LIMIT ?"
```

职责划分变了：**我们只负责切块与丢 unigram，排序交给引擎的 BM25**（InnoDB 全文仿 Sphinx，算法基于 BM25 + TF-IDF，见 MySQL 手册）。
⇒ 不再需要拼布尔式，也就不需要新写算子消毒（现有 `tokenize_query` 那套 `+ - > < ( ) ~ * " @` 清洗维持原样即可）。

#### 三条铁律（缺一条就退步）

1. **单字必须兜底**：chunk 长度为 1 时退回 unigram，否则 bigram 集为空（实测「体」两路都是第 37 名，行为不变）。
2. **不要融合**：等权（4.5→8）、RRF k=10/60（4.5→8）、D 为主 + B_top5 加成（4.5→7）——三种融合**全部更差**。
   原因：B 的行基本是 D 的子集，给子集加固定分等于把排第 3 的行压到第 8。**单跑 D。**
3. **停止词表仍必需**：D（有停词）8/8 vs E（无停词）7/8。NL 模式是 OR 语义，高频词会淹没结果——停词表的作用从「解开 AND 死锁」变成「压住 OR 噪声」。

#### 必须保留的护栏

- **`score/top` 归一化**：手册明写 MATCH 分数**不可跨查询比较**。
- **空表达式兜底**：`if not expr.strip(): return []`，绝不因切块返回 0。
- **不要指望 50% 阈值**：那是 MyISAM 的规则；InnoDB 没有。实测 `的` 走 NL 模式命中 101,895 行。

#### 已知软肋（不靠工程解，靠 schema）

「找这段话的上下文讨论，一定有」D 排第 **41**（B 排第 9）——「讨论」这种高频 bigram 在 OR 语义下淹没结果。
解法**不是**回头做融合（已证伪），而是 #11 里那条「**一个主题一次调用**」的 schema 规则。

#### 待实施清单（覆盖 #11 的对应项）

- [ ] `tokenizer` 增 `iter_chunks()` 切块器（索引侧/查询侧共用），`build_cjk_boolean` 改为产出 NL 模式的空格分隔 bigram 串
- [ ] `omnibus._fulltext` 与 `retrieval._fulltext_search` 改 `IN NATURAL LANGUAGE MODE`
- [ ] 单字兜底 + 空表达式兜底 + 保留 `score/top` 归一化
- [ ] 明确写进注释：**禁止与 AND 路做任何形式融合**（含 RRF）

---

### #17 口径修正（2026-11-07 · 同日，用户补充）

> **状态：已决策。取代 #17 里「上限不设」那一句。**

#### 最终契约

```
每路取数    ：limit 条（★ 同分不截断 —— 宁可略多于 limit，也不把并列组切一半）
去重前合计  ：≤ 3 × limit（三路）
去重        ：按 (source, record_id) 折叠，保留最高分，matched_by 合并
去重后下限  ：< limit 条不同记录 ⇒ 从各路更低排名递补（加深取数）
去重后上限  ：3 × limit（按分数截断，同分不截）
```

#### 与 10-25 拍板的关系（不是推翻，是收紧）

- 10-25 反对的是「**截断回 limit**」（5 条），理由是宽取后窄截会把「只有某一路命中、分数偏低」的独特结果挤掉。
- 本次把上限设为 **3 × limit**（15 条），仍远大于 limit ⇒ 10-25 的核心（不截断回 limit）**不变**。
- 新增的是**下限**（≥ limit 条不同记录），解决 #17 的原始问题：去重让结果变少。

#### 待确认（唯一一处）

「总计 3×limit」按**路**计还是按 **(源, 路)** 计？

| 口径 | 上限 | 现状代码 |
|---|---|---|
| 按路（三路合计） | 3 × limit | 需改 `hard_cap` |
| 按 (源, 路)（3 源 × 3 路） | 9 × limit | 现为 `max(limit*len(mds)*3, 60)` |

⚠️ 三个源（draftpad / messages / compactions）× 三个路（vector / fulltext / like）是 9 个取数单元。
若坚持 3×limit，则需把 `per_route_n` 从「每 (源,路) 取 limit」改为「每路跨源合计 limit」，这会改变各源的名额分配。

**拍板（2026-11-07）**：按 **(源, 路)** 计 —— **维持现状代码**（3 源 × 3 路 = 9 个取数单元，最多 9×limit），只把 `hard_cap` 收紧为 `limit × 源数 × 路数`（同分不截）。不引入按路分配名额的新机制。

---

### 已拍板（2026-11-07 · 数据修复：tokenizer 混合口径 + LIKE 定位）

| # | 问题 | 决定 | 依据 |
|---|---|---|---|
| 13 | `body_tokenized` 是**两代正则的混合口径** | **全量重算 + 重建 FULLTEXT 索引**，且**与 #12 的 vector 补嵌一块办**；**性能优先** | 10-25 前的行用旧正则：`Muérdago` → `Mu` + `rdago`（变音字符被 `findall` 静默丢弃）；10-25 后的行用新正则：CJK 紧邻拉丁时被吞成单 token。实测：tokenized 含完整 `muérdago` 的只有 213 行，body 有而 tokenized 没有的 **75 行**。两代病按行分布不同 ⇒ **任何「只补增量」的做法都会留一半脏数据** |
| 14 | LIKE 路（`ORDER BY guid DESC` = 随机、`score=1.0` 恒定） | **保留**，但**退出排序**：固定低权重、不参与 top-N 竞争、只作 filter/补充集。**密度法撤回** | ①保留的真实理由是**子串匹配**（`v30`、`13228v3`、`muerdagos`）与**脏 tokenized 兜底**（75 行只有 LIKE 捞得到），**不是** collation —— 实测 LIKE 与 FULLTEXT **都**折叠（283/283、5517/5517、251/251、190/190）②密度法 = `tf/len`，是 BM25 的退化形式（缺 IDF、缺 k1/b 饱和），而全文路已有引擎的 BM25 分数 ⇒ 拿伪分去融合等于掺噪声 |

#### #13 的修法与边界（实测已验）

```python
_CJK_SPLIT_RE = re.compile(f"([{_CJK_HAN}]+)")   # 只切汉字段
_WORD_RE      = re.compile(r"\w+")                # 非汉字段里取 \w+

def iter_chunks(text):
    """汉字段 / 非汉字段 交替产出。索引侧与查询侧共用这一个切分器。"""
    for part in _CJK_SPLIT_RE.split(text):
        if part:
            yield bool(_CJK_SPLIT_RE.fullmatch(part)), part
```

12 类语料实测：**俄文 / 希腊文（含词尾 ς）/ 带变音拉丁 / 纯汉字 / 全角标点 / emoji ZWJ / 天城文 / 下划线标识符 / 连字符串 —— 行为完全不变**；只有「拉丁/数字紧邻汉字」四类变好。
性能：800 条真实正文（2.9 MB）旧 0.16s / 切分版 0.19s（+19%），相对索引重建可忽略。

#### #13 的性能红线（用户明确强调）

- 重算 = 20.6 万行 × `MEDIUMTEXT`（2.4 GB）逐行 UPDATE + FULLTEXT 重建。按 README：MariaDB 上 FULLTEXT DDL **rebuild 整表**（历史实测 91 min + 82 min）。
- **必须与 #12 的 `ADD VECTOR INDEX` 合并在同一停机窗口**，避免两次全表重建。
- 顺序：`停写入 → 重算 tokenized → 重建 FT 索引 → 建 VECTOR 索引 → 补嵌 → 验收`。
- 验收水位（可复核，别信「重建完了」）：`body LIKE '%muérdago%' AND body_tokenized NOT LIKE '%muérdago%'` 应从 **75 → 0**。

---

### 已拍板（2026-11-07 · 手术方案：一次重建 + 存量清理 + 遗留处置）

| # | 问题 | 决定 |
|---|---|---|
| 18 | 存量脏数据（12,220 `inbox/spliced`、394 `system/message`、244 `compaction/summary`、样板文本） | **清理**。理由：反正要对表做大手术，**搬数据时不搬**（等价于 DELETE 但零额外成本，不用再跑一次 DELETE + 重建索引） |
| 19 | 升级适配的落地形态 | **都搞**：独立 CLI + `melquiades status` 子命令 + **pytest 用例**（接进 `PUBLISH-PLAN.md` 已定的「升级后第一动作」） |
| 20 | 工作区映射未覆盖的 13.8% 行 | 显示「**未分类工作区**」，**不参与 boost**，绝不猜 |
| 21 | HNSW recall 验证 | 同意，但**最后再做**（等表结构定稿、向量灌完） |
| 22 | `embedded` 标记可靠性 | **先测**（schema 注释记着 29/103 行零向量既没被 `!=` 排除也没被 `=` 找到，断点靠它不可靠就是断点失效） |
| 23 | `innodb_ft_min_token_size` | **离线改**（改 my.ini + 重启，`SET GLOBAL` 静默不生效），与本次手术同窗口 |
| 24 | 命中头显示 | **完整 64 位 guid**（与后端「messages 输出完整 guid」的设计意图对齐） |
| 25 | `_message_context_text` 抬头缺工作区/标题 | 加**探测装置**（能查到会话元信息的接口），抬头一并带上 |
| 26 | 返回侧诊断的判据 | 按文献与实践（见下），**不要自创阈值** |

#### #18 的实现要点

「清理」不写成 DELETE，而是**搬数据时过滤**：影子表只搬白名单事件类型，脏行自然消失。
省掉一次 DELETE（2.4 GB 写放大）+ 一次索引重建。

#### #26 的文献依据（零结果诊断）

- **每条零结果都是一次决策**：按**成因**分类（词汇缺失 / 语料缺口 / 拼写与形态 / 算子问题），不同成因对应不同修法。
- 实践共识：**说清「没匹配到什么、搜的是什么」**、**回显被解释后的查询**（让人看出错别字或误加的过滤）、**先放宽过滤再放宽查询**、**修法要作为「可点的动作」而不是「建议」**、**绝不为了不显示空态而编结果**。
- 理论对应 **Query Performance Prediction**：pre-retrieval 预测量（仅凭查询词与集合统计）与 post-retrieval（Clarity Score 等）。本项目已有 `_query_health`（`sep5` 分离度、`paths_hit` 路数）——**属 post-retrieval 一类，应合并而不是另起一套**。

#### 与既有实现的合并要求

`retrieval._query_health()` 已经在做 QPP（`flags` 机理级 / `soft_flags` 连续信号 / `numbers` 原始值），且**明确只报数不改行为**。
新的零结果诊断必须**接进同一个出口**，禁止另建一套提示通道。

---

### 已拍板（2026-11-07 · 补嵌范围与备份形态）

| # | 问题 | 决定 | 依据 |
|---|---|---|---|
| 27 | 补嵌范围 | **只嵌核心正文**（`user/message` + `assistant/message` 的 `kind='text'`）。**reasoning 不嵌**，工具输出不嵌 | 用户原话：「思维链过程不做嵌入……本质上思维链过程不引入特别多的新信息，这是信息论 GIGO 原理告诉我们的」。它们由全文路 + LIKE 路兜底。**体量对照：核心正文 13,156 行 / 18.2 MB（占待补嵌 426 MB 的 4%）；reasoning 42,773 行 / 162 MB；tool/* 106,599 行 / 221 MB** |
| 28 | 备份形态 | **禁止 `mariadb-dump` / `mysqldump`**；影子表方案下**旧表即备份**（RENAME 成 `dsh_message_old_<ts>`，含全部索引，回滚=反向 RENAME）。可选加一层 `CREATE TABLE ... AS SELECT` 冷备 | **MDEV-35221（官方 issue 仍开）**：vector 值被 dump 成字符串，restore 在非严格模式下把非法字符串**静默转成零向量**，不报错。实测 CTAS 保真：`HEX(v)` 逐字节相同、列类型 `vector(1024)` 保留、**但索引不复制** |

#### #27 对时间窗的影响

| 范围 | 体量 | 估算（TPM 500k） |
|---|---|---|
| 核心正文 | 18.2 MB | **约 15 分钟** |
| + reasoning | 180 MB | 约 1.5–2 小时 |
| 全量 | 426 MB | 约 4 小时（塞不进 2–3 小时窗口） |

⇒ 窗口内只补核心正文；其余保持 `embedded=0`（查询侧已有 `WHERE embedded = 1` 过滤，功能不退化），后续由断点续跑补齐。

#### 工具形态（用户拍板）

**写离线 Python 工具**，不写一次性脚本。理由：步骤多、可中断、需断点续跑、需逐步验收。
停机方式：**关掉 DSH** ⇒ MCP / 桥 / 收割器一并停止，天然窗口（无需单独停收割器）。

#### #28 更正（2026-11-07 · 官方已修，本机实测通过）

> **原记录「禁止 mariadb-dump」作废。** 该结论基于旧版本行为；官方修复与端到端实测都表明本机安全。

**官方修复**：MDEV-35221 修在 **MariaDB 11.7.2**（2025-02-13 发布）。
修复内容（commit `40d39b1`）：**对 `GEOMETRY` / `BIT` / `VECTOR` 类型强制 `--hex-dump`**，走与 BLOB 相同的路径。
11.7.2 的 Release Notes 在「Vector search」下明列：「Vector values become zero after mariadb-dump / restore, impacting data integrity for vector columns. (MDEV-35221)」。

**本机端到端实测（MariaDB 12.3.2 + `MariaDB1232/bin/mariadb-dump.exe`）**：

```
建探针表（2 行 × VECTOR(1024)）→ mariadb-dump（默认参数）→ DROP TABLE → 从 dump 恢复
结果：HEX(v) 与原始**逐字节相同**，无清零；dump 默认即输出 0x 十六进制字面量
```

**结论与选型**：

| 方式 | 是否官方认可 | 保真 | 成本 | 适用 |
|---|---|---|---|---|
| **旧表即备份**（RENAME 成 `_old_<ts>`） | — | 100%（原表原样，含索引） | **零** | 本次首选，回滚=反向 RENAME |
| `CREATE TABLE ... AS SELECT` | — | 100%（实测 HEX 逐字节相同） | 3.6 GB 空间、几分钟 | 加一层保险 |
| `mariadb-dump`（≥ 11.7.2） | **是** | 100%（本机实测） | dump 约 **3 GB 文本**、恢复几十分钟 | 异机归档 |
| `mariadb-backup`（物理） | **是** | 100%（文件级） | 需 datadir 访问（在 Windows 侧） | 整库物理备份 |
| `mariadb-dump`（< 11.7.2） | 有 bug | **静默清零** | — | **禁用** |

⚠️ 相关坑（不影响本次）：**MDEV-35258**「Mariabackup does not work with **MyISAM** tables with vector keys」——限定 MyISAM；本表是 InnoDB。

---

### 已拍板（2026-11-07 · 升级窗口标志）

| # | 问题 | 决定 |
|---|---|---|
| 29 | 升级窗口如何对「后来的程序」广播 | `config.yaml` 增 **`maintenance`** 段：`enabled` / `reason` / `since` / `harvest_enabled` / `messages_table`。**收割器自己看标志就停**（不依赖「人记得关 DSH」） |

#### 三个作用（缺一个就只是装饰）

1. **收割器自停**：`harvest/scheduler.py::_harvest_allowed()` 每轮检查；关闭时打一次 WARN（含 reason/since）后跳过，不刷屏。
2. **误启动的 MCP 能看见**：`messages_table` 非空 ⇒ 检索侧可改读重建中的表（**待接线**）。
3. **后来的人/程序一眼知道**：`reason` + `since` 写进配置，`melquiades status` 可回显。

#### 为什么必须挡在调度器而不是 runner

窗口内写进旧表的行，会在 RENAME 之后表现为「最新几分钟的内容消失」——**静默丢数据**。
挡在调度器 = 在「决定要不要跑一轮」这一层就拦掉，成本最低、语义最清楚。

#### 默认全关

没有 `maintenance` 段时行为与从前完全一致（`harvest_enabled` 缺省 true）。

---

### 实施记录（2026-11-07 · #15 / #24 / #25 已落地代码）

| 项 | 实现 | 验证 |
|---|---|---|
| #15 工作区 | 新增 `melquiades/session_meta.py`（`SessionMetaIndex`）：`workspace.json` 全量加载（16 KB/个 home，映射 170 会话 / 3 ms），标题**懒加载 + LRU**（单文件最大 1.8 MB，实测 4–22 ms）。`omnibus.search()` 增 `workspace_mode`（off/boost/filter）+ `current_cwd`；`boost` 为乘性 ×1.25（乘性不改变分数分布形状，加性会让低分项永远追不上） | 实测 boost：冰质星球结果从第 3 → 第 2 并多挤进一条（4 条加权）；filter：20 → 14 条（无工作区信息的保留） |
| #24 完整 guid | `client.js` 去掉 `slice(0,16)`，改渲染完整 64 位 + mono 样式 + `word-break` | `node --check` 通过 |
| #25 抬头带工作区/标题 | `server._message_context_text` 抬头改为 `工作区 › 标题 · session-xxx · 目标 Lxxxx` | 实测：「会话 冰质星球与是啊，吃什么 › 设定落实（77） · session-35e1830e-… · 目标 L2899」 |

#### 两处踩坑（记下来免得重犯）

1. **`cfg.dsh_home` 在 MCP 进程外取不到** —— 它由 `DSH_VERSION` 派生，离线脚本/测试里会回落到 `~/.dsh`（不存在），索引静默为空。
   修法：`from_dsh_home(dsh_home, harness_dir)`，**以 harness_dir 为根按规则 glob `dsh-domus*`**（追规则不追名单）。
   这正是 config.yaml 里 2026-09-26 那次「写死 dsh-domus → 改名当天收割静默归零」的同一类坑。
2. **`session_id` 有裸 uuid 形态**（库里 6,859 行），workspace.json / projcache 文件名都带前缀。
   不归一会表现为「明明有这场会话，工作区却显示未分类」。已在 `normalize_session_id()` 统一。

#### 未做（继续留在待办）

- `tiered`（分层展示）：需要 UI 分组，属呈现层，暂缓。
- #17 去重后递补：当前仍是「去重后全给」，下限保证未实现。
- `search_OMNIA` 的 schema 文案（查询写法 / 过期事实）尚未改。

#### 生效条件

`omnibus` / `server` / `session_meta` 的改动**需要重启 MCP** 才生效（MCP 常驻进程）；
`client.js` 的改动需要**重启 DSH + 硬刷新浏览器**。

---

### #29 补强（2026-11-07 · 守卫必须覆盖**所有**入口，不只是调度器）

> 起因：用户指出「我关了 DSH，按理说就该没人管它了」——而初版只挡了调度器。

#### 收割的三个入口（漏一个就白挡）

| 入口 | 触发条件 | 初版是否挡住 |
|---|---|---|
| 定时器 `HarvestScheduler` | 每 60s | ✅ |
| **启动扫** `_start_triggers → startup_scan` | MCP 启动 | ❌ |
| **工具搭车** `_piggyback → tool_scan` | 任何工具调用 | ❌ |

⇒ 守卫**下沉到 `HarvestRunner.run_once()`** —— 三个入口最终都走它，一处即全覆盖。

#### 还有一类更危险的：启动期的 DDL

`server` 启动会跑 `_store.ensure_tables()` 与 `ensure_all_vectors(_db)` —— **都是 ALTER**。
窗口里表正在被重建，两者相撞轻则互相等待，重则改坏重建中的表结构。
⇒ `maintenance.enabled=true` 时**整段跳过**，并打一条 WARN 说明原因。

#### 桥的行为：明确拒绝，不静默降级

窗口内旧表虽完整但已过时、v2 是半成品 —— 无论读哪张都会误导。
`do_search` 直接返回 `{ok:false, error:"维护中（since）：reason"}`，
UI 会显示这句原文。**宁可说「不可用」，也不给一个看起来正常的错答案。**

#### 实测（维护标志当前为 true）

```
WARNING harvest blocked by maintenance flag (reason='dsh_message 重建…' since=2026-11-07T09:12:54+08:00)
run_once 结果: trigger=manual files_seen=0 errors=['harvest blocked by maintenance flag']
```

⇒ 现在**开 DSH 也不会干扰迁移**：不收割、不跑 DDL、检索直接说「维护中」。

---

### 长任务执行框架（2026-11-07 · 用血换的，别再踩）

> 触发场景：`ALTER TABLE ... ADD INDEX`（30–40 分钟）、`UPDATE ... JOIN`（3.6 万行向量）。
> 当时的表现：**Ctrl+C 完全没反应**，最后只能 `kill -9`。

#### 一、判据：看有没有「外部取消句柄」

| 情形 | 方案 | 例子 |
|---|---|---|
| 有外部取消句柄 | **子线程 + 外部取消** | DB 查询（`KILL <conn_id>`）、HTTP（断连）、`CancelSynchronousIo` |
| 无取消句柄 / CPU 密集 | **子进程 + SIGKILL** | 第三方 C 库、不可中断 syscall、纯计算 |

**为什么线程在第二种情形下无解**：线程不可无条件终止。Win32 文档明写 `TerminateThread`
应避免（不释放线程栈、不解除持有的锁）。只有进程能被内核无条件回收。

#### 二、三条实测教训

1. **`cur.execute(ALTER)` 阻塞期间 Ctrl+C 收不到** —— Python 只在**字节码间隙**处理信号。
   所以「按了没反应」不是错觉，是机制。
2. **换成子线程也不够** —— 信号**只投递给主线程**，子线程卡在 C 调用里同样收不到。
   正解：主线程轮询（`time.sleep` 期间可响应）+ 中断时**从服务端 KILL**。
   这套在 Win32 里有专名：`CancelSynchronousIo(hThread)` —— 从外部取消阻塞调用。
3. **没有百分比上报时不要显示 0%** —— MariaDB 只对部分 stage 报 `PROGRESS`
   （`copy to tmp table` 有，普通 `UPDATE` 没有）。显示 0% 会让人以为卡死；
   应切到不确定进度条（`已用 00:02`）。

#### 三、进度从哪来（别再说「数据库不给」）

MariaDB **5.3（2011）** 起就有：

```sql
SELECT STAGE, MAX_STAGE, PROGRESS FROM information_schema.PROCESSLIST WHERE ID = ?
```

`SHOW PROCESSLIST` 也有 `Progress` 列；服务端还会每 5 秒主动推送。
**是我们从来没去读。** MySQL 8.0 走另一套（`performance_schema.events_stages_current` 的
`WORK_COMPLETED` / `WORK_ESTIMATED`）。

#### 四、父进程职责（**待补，2026-11-07 未做**）

`tools/run_rebuild.sh` 本来就是「父进程调度子进程」的结构，问题不在架构，在
**父进程没把子进程当子进程管**：只 `| tee` 了一下，没 `trap`、没转发信号、没超时。

要补的是父进程：`trap SIGINT` → 转发给子进程 → 子进程被杀 → 连接断 → 服务端回滚。
**这比在每个 Python 步骤里各写一遍线程逻辑更符合「进程」的本意** —— 职责放回它该在的层。

#### 五、编排层不再用 bash

bash 到这个复杂度（互斥锁、信号转发、断点续跑、步骤状态、失败分诊）**必然变地狱**：
没类型、没异常、字符串拼 SQL、`PIPESTATUS`/`set -e`/`trap` 三者互相干扰。

**决定：编排逻辑搬进 Python**（一个 `orchestrator.py`：步骤表 + 状态机 + 信号处理 + 子进程管理）。
bash 只保留一层薄壳（`exec python3 tools/orchestrator.py "$@"`）。

#### 六、自我更正（别把标准操作当障碍）

我一度把「子进程的代价」列成三条障碍（状态序列化 / 错误信息降级 / 启动开销）——
**那是 IPC 的标准操作，不是障碍**。现代 OS 开进程很便宜：
`fork` + COW（Linux）、Chrome 的 zygote（Windows/macOS 同样有预启动进程），
Chrome 靠 Site Isolation 开几十上百个渲染进程，代价完全可接受。

真正的判据只有一条：**任务时长 >> 启动开销** ⇒ 子进程划算。
粗粒度任务（一个步骤几分钟）正是进程完胜、线程完败的区间。

---

### 长连接必须在 Windows 侧跑（2026-11-07 · 由 WSL 110 定案推导）

#### 现象

`ALTER TABLE dsh_message_v2 ADD INDEX ...` 跑到 30 秒报
`mariadb.InterfaceError: Lost connection to server during query`，
**服务端错误日志一个字都没记**（最后一条是上一轮的 FTS 警告）。

#### 归因（复用 2026-10-31 的 110 定案，不重新查）

> 病因不是丢包，是 **WSL mirrored 的 loopback relay 改错端口**：
> WSL#40343（SYN-ACK 回错临时端口）、#41137（relay dial 错端口 / 14 字节校验和偏移）、
> #10855（connect 挂到超时）。当时的缓解是 `connect_timeout` 30→5 + 重试 7 次，
> 但那条**只治建连阶段**（定案决策 4：查询途中不重放，因为无法判断服务端是否已执行）。

`ALTER` 要**一条连接挂 30 分钟**。建连重试再多次也救不了中途断连。

#### 决定

**重建工具在 Windows 原生跑**，不走 WSL：

```
<仓库根>> tools\run_rebuild.cmd index --with-vector
```

- MariaDB 在 Windows 侧（数据目录在本地盘），Windows 直连 `127.0.0.1:9002` **零跳**
- WSL 侧则要跨 mirrored relay 两跳，任何一次抖动都让 30 分钟的 ALTER 归零
- 已实测：Windows Python 3.12.10 + `mariadb` / `tqdm` / `httpx` / `yaml` 齐备

#### 两个编码坑（`.cmd` 特有）

1. **`.cmd` 文件必须 ASCII-only** —— cmd.exe 按 GBK 解析，UTF-8 中文注释会破坏行解析
   （实测：注释里的中文被当成命令执行，报「不是内部或外部命令」）。
   中文文档放 DESIGN.md 和 Python 里，别放 `.cmd` 里。
2. **`chcp 65001` + `PYTHONUTF8=1`** —— 否则 Python 输出的中文在 GBK 控制台是乱码。

#### 通用规则（以后所有长任务都适用）

> 预计**单条连接存活超过 1 分钟**的 DB 操作，一律在 Windows 侧执行。
> 短查询（秒级）在 WSL 侧无所谓——建连重试已经能兜住。

---

### ⚠ 更正：上面那条「长连接必须在 Windows 侧跑」的归因是**错的**

**真正的病因：`config.yaml` 的 `read_timeout: 30`。**

`mariadb.connect(read_timeout=30)` 限制的是**任何一次从 socket 读响应**的等待，
不是「空闲超时」。所以一条跑 30 分钟的 `ALTER` 会在**第 30 秒**被**客户端自己**掐断：

```
index [0/0] copy to tmp table   :  已用 00:30
mariadb.InterfaceError: Lost connection to server during query
```

**判据**：WSL 侧和 Windows 原生侧**死在同一个 30 秒**。
如果是 relay 的问题，绕开 relay 的 Windows 原生跑法就不该同样死在 30 秒。
我把两次相同的时间戳当成了两次不同的偶发，**没有去比「两次的共同点」**——这是误诊的直接原因。

#### 修法

长 SQL 用**独立连接**，读超时放宽到 2 小时：

```python
LONG_SQL_TIMEOUT = 7200
mariadb.connect(..., read_timeout=LONG_SQL_TIMEOUT, write_timeout=LONG_SQL_TIMEOUT)
```

实测：`SELECT SLEEP(45)` 修前 30 秒报 `Lost connection`，修后 **46.2 秒正常返回**。

#### 保留的结论

1. **Windows 侧跑仍然是对的**（只是理由不同）：MariaDB 在 Windows，WSL 侧要跨 mirrored relay，
   而 relay 有已知未修 bug（WSL#40343 / #41137），建连阶段确实会偶发 110。少两跳总是好的。
2. **`run_rebuild.cmd` 保留**（`.cmd` 必须 ASCII-only + `chcp 65001` 两条坑也是真的）。
3. **relay 的 110 定案本身没错**，它只是**不是这次**的病因。

#### 顺带发现的第二个坑：客户端断开，服务端不停

WSL 那轮 `Lost connection` 之后，**服务端那条 ALTER 继续在跑**（`id=1273918`，
跑到 698 秒仍在 `copy to tmp table`，临时表以 ~1.1 MB/s 增长）。
它一直持着 `dsh_message_v2` 的元数据锁，于是**新的 ALTER 只能排队等**
（`state = Waiting for table metadata lock`）。

⇒ 断连 ≠ 取消。要么显式 `KILL <id>`，要么等它跑完。
这也解释了 `run_sql_with_progress` 里那个 `KILL` 分支为什么必要。

---

### 长 SQL 的进度上报：官方怎么说的（2026-11-07 查证）

> 起因：`ALTER TABLE ... ADD INDEX ×7`（含 FULLTEXT + VECTOR）跑了一小时，
> `PROCESSLIST` 的 `STAGE / MAX_STAGE / PROGRESS` **恒为 0/0/0.000**，
> `TMP_SPACE_USED` 也恒为 0。当时的结论是「靠临时表文件大小兜底」。
> 用户追问：官方有没有说过这事？

#### 一、进度上报有官方明文的**重大局限**

MariaDB 文档 `Progress Reporting` 页的 **Limitations** 小节（原文）：

> Although the above commands support progress reporting, there are some limitations to what
> progress is reported. To be specific, when executing one of these commands against an
> **InnoDB table with `ALGORITHM=INPLACE`（which is the default）, progress is only reported
> during the merge sort phase while reconstructing indexes.**

支持的命令是 `ALTER TABLE` / `CREATE INDEX` / `DROP INDEX` / `LOAD DATA INFILE`
（以及 Aria 的 CHECK/REPAIR/ANALYZE/OPTIMIZE TABLE）—— **我们的语句在名单里**，
但**在 InnoDB INPLACE 下只有 merge sort 阶段报**。

⇒ 「这么长的操作总该有进度吧」这个直觉是错的，**官方早就写了会没有**。

#### 二、`TMP_SPACE_USED` 恒为 0 是**设计如此**

该列自 **MariaDB 11.5** 引入。PROCESSLIST 文档页只写「见 Limiting Size of Created Disk
Temporary Files and Tables Overview」，**没有列出排除项**；排除项在功能说明里（原文）：

> The temporary space is counted for: All SQL level temporary files...
> **It does not include engine internal temporary files used for repair, alter table,
> index pre sorting etc.**

⇒ `ALTER TABLE` 的引擎内部临时文件**不在统计范围**，所以 `TMP_SPACE_USED = 0` 不是 bug。

#### 三、官方**没写**的那部分（我们的实测补充）

文档只解释了 INPLACE 的情形；我们这次走的是 **COPY 算法**
（`state = copy to tmp table`，因为要加 **VECTOR** 索引，见下方实测），
官方没有明说 COPY 下 `copy to tmp table` 阶段是否上报 —— **实测：完全不上报（0/0/0）**。

#### 四、可移植的进度源（结论）

| 来源 | 可靠性 |
|---|---|
| `PROGRESS` / `STAGE` | 仅部分算法/阶段；本次全程 0 |
| `TMP_SPACE_USED` | **官方明确不含 ALTER**，恒 0 |
| **`#sql-alter-*.ibd` 文件大小 / 源表大小** | ✅ 唯一始终有效的信号 |
| `TIME` | 只能报「已经多久」，不能报「还剩多少」 |

过滤临时表文件时要**排除 `#i#` 开头的 FTS 附属文件**（同一 ALTER 会生成多个），
否则会把附属索引文件的大小当成主表进度。

#### 五、教训

1. **别假设长操作一定有进度** —— 先翻官方文档的 **Limitations** 小节，它常被放在
   正文之后、看起来像脚注。
2. **兜底信号要选「操作系统层面可见」的东西**（文件大小、日志增长），
   而不是「应用层自报」的字段 —— 后者随时可能因为算法分支不实现而恒为 0。
3. 文档没说的地方要**标成实测结论**，别混写成官方事实。

---

### 谁把 ALTER 逼成 COPY 的（2026-11-07 实测 + 官方条文）

> 背景：`ALTER TABLE dsh_message_v2 ADD INDEX ×6 + FULLTEXT + VECTOR` 走了 COPY 算法，
> 逐行维护 8 个索引结构，跑了 50+ 分钟。先前的归因是「FULLTEXT + VECTOR 都不支持 INPLACE」，
> **这个归因是错的。**

#### 一、实测（小表，逐个 `ALGORITHM=INPLACE` 试探）

```
✓ INPLACE 接受: BTREE 普通索引
✓ INPLACE 接受: FULLTEXT（无 FTS_DOC_ID）
✗ INPLACE 拒绝: VECTOR
    OperationalError: ALGORITHM=INPLACE is not supported for this operation. Try ALGORITHM=COPY
```

**结论：逼出 COPY 的是 VECTOR，不是 FULLTEXT。**

#### 二、官方条文

`CREATE INDEX` / `ALTER TABLE` 文档「Adding a Fulltext Index」段（原文要点）：

> InnoDB supports adding a FULLTEXT index to a table with ALGORITHM set to INPLACE.
> The table is not rebuilt in some cases. However:
> · Adding a FULLTEXT index to a table that **does not have a user-defined FTS_DOC_ID column
>   will require the table to be rebuilt once**（系统会补一个隐藏的 FTS_DOC_ID 列）
> · Only one FULLTEXT index may be added at a time when ALGORITHM is set to INPLACE
> · If a table has more than one FULLTEXT index, it cannot be rebuilt by any ALTER TABLE
>   when ALGORITHM is set to INPLACE

⇒ FULLTEXT 是「INPLACE，但内部可能重建一次」；**VECTOR 是直接不支持 INPLACE**。

#### 三、那「拆开建」能省时间吗——**不能，反而更慢**

我一开始的建议是「先建 6 个 BTREE，再 FULLTEXT，最后 VECTOR」。算一下就知道错：

```
拆开：  INPLACE 建 7 个索引  +  COPY 重建整表（连那 7 个一起重建）
现在：  COPY 重建整表（8 个一起建）
```

COPY 那一步的代价**一样**，拆开只是多干一遍前戏。**当前的一次性 ALTER 反而是最优的。**

#### 四、真正的省法（下次用）

**建表时就带上全部索引，然后一次性 load** —— 只走一遍，不用「load 完再重建」。

代价要说清楚：
- load 时每行要维护 8 个索引 → **load 会明显变慢**（现在是 1000 行/s 无索引）
- 后续 `UPDATE ... SET embedding=...` 会**逐行维护 HNSW 图** → 补嵌会非常慢

所以这是个权衡：**省一次全表重建，换 load 和补嵌变慢**。数据量大到 20 万行以上时才划算。

#### 五、教训

1. **别用「大概是 XX 不支持」来归因** —— 一条 `ALTER ... , ALGORITHM=INPLACE` 打过去，
   引擎会直接告诉你哪个不支持，比查文档和猜都快。
2. 官方文档里 FULLTEXT 的限制写得很细（三条），但**没有任何一处说 VECTOR 不支持 INPLACE**——
   那条信息只在错误消息里。**错误消息本身是一等公民的文档。**
3. 我先前把锅推给 FULLTEXT，是因为「加了两个新东西，那就两个都有问题」这种懒惰推理。

---

### 下一版架构：垂直分表（2026-11-07 实测审计后定）

> 动机：COPY 重建是**按表体积线性**的（实测 1.42 MB/s 建页速率）。
> 表里只要有「大字段 + 向量列」，任何 schema 变更都要为它们付全款。
> 垂直分表把这些成本从主表上剥掉。

#### 一、审计结果（实测，不是估）

**melquiades.dsh_message**（4.14 GB / 206,652 行）

| 列 | 体积 | 说明 |
|---|---|---|
| `embedding` | **807 MB** | ⚠️ 只有 34k 行（16.5%）有真向量，其余 17 万行各存 4 KB 零 ⇒ **约 700 MB 纯废料** |
| `body_tokenized` | **719 MB** | 纯派生数据，可随时重算 |
| `body` | 445 MB | 真载荷，必须留 |
| 其余 18 列合计 | ~50 MB | |

**memoria_del_hielo.documents_book_chunk**（13.1 GB / 654,315 行）—— 问题严重得多

| 列 | 体积 | 说明 |
|---|---|---|
| `content_tokenized` | **6,024 MB** | 派生数据 |
| `embedding` | **2,556 MB** | 全部行都有 |
| `content` | 2,480 MB | 真载荷 |
| 其余 10 列合计 | ~200 MB | |

**memoria_del_hielo.embedding_cache**（871 MB / 28,636 行）

| 列 | 体积 | 说明 |
|---|---|---|
| `vector_json` | **621 MB** | ⚠️ **向量以 JSON 文本存储，平均 22.7 KB/行**；改 `VECTOR(1024)` 只需 4 KB ⇒ **可立刻省约 500 MB**，与分表无关 |

#### 二、目标形态：四张表，按访问模式切

```
dsh_message        主表（热、小）：guid + 全部元数据 + body_bytes + body_sha256
                   ⇒ 20 万行约 60 MB，schema 变更秒级
dsh_message_body   冷：guid + body（445 MB）—— 只在展示命中时按 PK 取
dsh_message_fts    派：guid + body_tokenized（719 MB）+ FULLTEXT 索引
dsh_message_vec    派：guid + embedding（**只存已嵌的行**，约 139 MB）+ VECTOR 索引
```

重建成本对比（按实测 1.42 MB/s）：

| 操作 | 现在 | 分表后 |
|---|---|---|
| 改元数据（加字段等） | 全表 4.14 GB，~50 min | 主表 60 MB，**秒级** |
| 重建 FULLTEXT | 随主表一起 | 719 MB 表，INPLACE |
| 重建 VECTOR | **逼整表走 COPY** | 139 MB 表，COPY 但**~2 分钟** |

**最关键的不是体积，是 COPY 的传染性**：只要 `VECTOR` 列在主表上，
`ADD VECTOR INDEX` 就强制整表 COPY（实测：`ALGORITHM=INPLACE is not supported`），
主表上**任何** DDL 都变成 50 分钟。把它挪出去，主表就恢复 INPLACE/INSTANT。

#### 三、两条必须写下来的决定

**① 只存有向量的行，不存全量。**
现在 191,913 行里有 151,215 行是零向量，照样进 HNSW 图 ——
**大量完全相同的零向量是 HNSW 的退化输入**，图结构会坏、召回会掉。
侧表只放 `embedded=1`，同时解决三件事：重建快、索引小、**ANN 质量变好**。

**② 不用 FOREIGN KEY，用 `guid` + 应用层保证。**
通用实践确实推荐「分表都引用主表主键」（OneUptime 等），但**我们的场景不适用**：

- 切换方式是 `RENAME TABLE dsh_message TO old, v2 TO dsh_message` —— 外键会跟着表名走，
  这个 swap 立刻变复杂，且旧表被 DROP 时会被外键拦住
- 外键禁止 `TRUNCATE`、拖慢批量插入（收割器每轮都在插）
- 白名单过滤会让行集合变化，外键检查会在重建时制造无谓失败

`guid = sha256(session|gens|line_no|block_path)`，**跨重建稳定**，天然适合当关联键。
完整性由收割器与工具保证，出问题用对账脚本查（`verify` 已有行数对账）。

#### 四、业界实践对照

垂直分表（vertical partitioning）的通行原则与我们一致：

| 原则 | 我们的对应 |
|---|---|
| **大字段独立**：TEXT/BLOB 必须拆出主表 | `body` / `body_tokenized` 拆到侧表 |
| **按访问频次分组**：常一起查的留一起 | 元数据留主表；body 只在展示时取 |
| 分表都引用主表主键 | `guid`（但不用 FK，理由见上） |

技术前提（InnoDB 官方文档）：`ROW_FORMAT=DYNAMIC` 下长 `VARCHAR/TEXT/BLOB`
**行内只存 20 字节指针**，实体在 overflow 页。所以我们表里的 `body` 本来就不撑大行内空间 ——
但 **overflow 页在 COPY 重建时照样要被复制**，所以分离它们对重建时间的收益是按体积算的，成立。

#### 五、迁移路径（不用重新嵌入）

```sql
-- 1. 向量搬家（原样复制，不重算）
CREATE TABLE dsh_message_vec (guid CHAR(64) PRIMARY KEY, v VECTOR(1024) NOT NULL,
                              model VARCHAR(64) NOT NULL DEFAULT '') ENGINE=InnoDB;
INSERT INTO dsh_message_vec (guid, v) SELECT guid, embedding FROM dsh_message WHERE embedded=1;

-- 2. FTS 搬家
CREATE TABLE dsh_message_fts (guid CHAR(64) PRIMARY KEY, body_tokenized MEDIUMTEXT,
                              FULLTEXT KEY ft (body_tokenized)) ENGINE=InnoDB;
INSERT INTO dsh_message_fts SELECT guid, body_tokenized FROM dsh_message;

-- 3. 主表瘦身（这一步会重建一次，仅此一回）
ALTER TABLE dsh_message DROP COLUMN embedding, DROP COLUMN body_tokenized;
```

顺带红利：**换嵌入模型变便宜** —— 侧表时代是 `DROP` 139 MB 的表、重嵌、重建 2 分钟；
现在是重算 19 万行 + 重建 50 分钟。

#### 六、优先级

| 项 | 收益 | 风险 | 建议 |
|---|---|---|---|
| `embedding_cache.vector_json` → `VECTOR` | **立刻省 500 MB** | 低（只需转换 + 改读写） | **先做这个** |
| `dsh_message` 拆向量 | 重建 50 min → 2 min | 中（改检索代码） | 下一轮 |
| `dsh_message` 拆 tokenized | 主表再减 719 MB | 中（全文检索要 join） | 与上同批 |
| `documents_book_chunk` 拆向量+tokenized | 13 GB → ~3 GB | 高（memoria 全链路） | 单独排期 |

#### 七、更正：`embedding_cache` 是**缓存**，不是数据

我一开始给的建议是「JSON → VECTOR 转换迁移」，**方向错了**——它是缓存，
迁移缓存数据毫无意义，**直接清空**即可。

实测（2026-11-07）：

```
条目 28,636 | 已过期 0 | 无 TTL 0 | 621.0 MB | 模型数 1 | 2026-10-25 ~ 2026-11-07
```

- 项目里**已有现成 CLI**：`src/cli/mhi_cleanup_emb_cache.py`（`--stats` / `--all` / 默认清过期）
  —— 按「以创造接口为耻，以复用现有为荣」，不该另写脚本。
- miss 路径已确认存在：`embedding.py::_cache_get_many` 未命中就重算并回写，
  所以清空**不会丢功能**，只会让下次用到时重算。

#### 但清空只是重置计时器

写入路径是 `json.dumps(vector)`（`embedding.py:314`），读回是 `json.loads`。
**清空后一个月又会长回 621 MB**（22.7 KB/行 vs 二进制的 4 KB，5.5 倍膨胀）。

真正的持久修法是改列类型 + 读写两处：

```python
# 写： json.dumps(vector)  →  VEC_FromText(?)
# 读： json.loads(row[...])  →  VEC_ToText(...) 或 VEC_ToBinary(...)
```

表空的时候 `ALTER ... MODIFY` 是瞬时的，所以**最省事的顺序是：先清空 → 再改类型 → 再改代码**。

#### 今天的结论

```sql
TRUNCATE TABLE memoria_del_hielo.embedding_cache;   -- 621 MB → 0，瞬时可逆（缓存会自己长回来）
```

代价要说清楚：28,636 条**全部是新鲜的**（0 条过期），清空后重算约 28 分钟，
但那是**懒加载**——用到哪条算哪条，不是一次性 28 分钟。

---

### ABC 三方案标定结果（2026-11-07 实测，12 条金标查询）

#### 金标集先验（避免拿错标尺）

12 条查询，目标会话 `35e1830e` / `b24095f7`。**每条查询的词在两个目标里都真实存在**
（`Lyfe` 16/33 行、`四支柱` 3/5 行、`尘埃等离子体` 2/0 行），所以指标有效，不是标错。

#### A：补 messages 向量路 —— **决定性**

| 配置 | @5 | @10 | @20 | MRR | 平均耗时 |
|---|---|---|---|---|---|
| vec 关 | 1/12 | 2/12 | 4/12 | 0.038 | 828 ms |
| **vec 开** | **5/12** | **7/12** | **10/12** | **0.243** | 836 ms |

9 条改善、1 条变差（`Lyfe` 18→23）、2 条不变。耗时几乎不变。
⇒ **已启用**（`SOURCE_SPECS['messages']['vec'] = True`）。

⚠️ 注意：建了 VECTOR 索引 ≠ 开了向量路。切换后第一次测只有 4/12，
因为 `vec` 标志还是 False —— **索引白建了**。这是最容易漏的一环。

#### B：按源归一化 —— **否决**

做法：每个源内部再除以该源最高分，使各源顶分齐平。

| 配置 | @20 | MRR |
|---|---|---|
| 不做（基线） | 8–10/12 | **0.225** |
| 做（ft=0.40~0.70 各档） | 10/12 | 0.087 ~ 0.116 |

召回没提升，**MRR 掉一半以上**。原因：除以源内最高分等于把「源内第一名」全部拉平，
而各源的绝对分本来就已经通过权重表达过偏好 —— 再归一化是**重复施加**，把权重信息抹掉。
⇒ **代码已删除**（不留开关，避免后来者误开）。

#### C：提高 fulltext 权重 —— **否决**

| ft 权重 | @5 | @10 | @20 | MRR |
|---|---|---|---|---|
| **0.40（现状）** | 5/12 | 7/12 | 9/12 | **0.237** |
| 0.45 | 5/12 | 8/12 | 10/12 | 0.205 |
| 0.50 | 5/12 | 7/12 | 10/12 | 0.188 |
| 0.55 | 5/12 | 6/12 | 8/12 | 0.153 |
| 0.60 | 5/12 | 7/12 | 11/12 | 0.168 |
| 0.65 | 5/12 | 7/12 | 10/12 | 0.161 |
| 0.70 | 4/12 | 7/12 | 9/12 | 0.155 |
| 0.80 | 3/12 | 6/12 | 10/12 | 0.153 |

- `@5` 在 0.40–0.65 **完全平坦**（5/12）
- `@20` 在 8–11 之间**非单调抖动** ⇒ 是噪声，不是信号
- `MRR` **单调下降**（0.237 → 0.153）

⇒ **维持 0.40**。

#### 教训：12 条查询调不动权重

我第一轮扫到「ft=0.70 → 11/12」就差点写进结论，第二轮细扫同一档只有 9/12。
**12 条样本的 ±1~2 条差异是噪声**。要调权重，金标集至少要有几十条，
否则就是在给噪声拟合参数。

#### 仍然失败的 2 条（不是权重问题）

| 查询 | 现象 | 猜测 |
|---|---|---|
| `Lyfe 四支柱是什么` | 完全捞不到 | 单独查「四支柱」排第 6，加上「Lyfe」反而消失 —— OR 语义下高频词把稀有词淹了 |
| `Lyfe` | 排名 18 → 23（开向量后略差） | 单 token 查询，向量路贡献了语义近似项 |

这两条要治得动**查询理解**（词权重 / 必需词判定），不是调融合参数能解决的。

---

### 垂直分表已实施（2026-11-07）

#### 结果

| 表 | 体积 | 内容 |
|---|---|---|
| `dsh_message` | **748 MB**（原 3,817 MB） | 元数据 + `body` |
| `dsh_message_fts` | 932 MB | `body_tokenized` + FULLTEXT |
| `dsh_message_vec` | ~163 MB | `embedding`，**只存已嵌的 40,698 行** |
| `dsh_message_old_20261107_091254` | 3,719 MB | 重建前的旧表（回滚用，观察几天再删） |

#### 检索质量：分表后**反而更好**

| 配置 | @5 | @10 | @20 | MRR | 耗时 |
|---|---|---|---|---|---|
| 同表（主表带 VECTOR 索引） | 5/12 | 7/12 | 10/12 | 0.243 | 836 ms |
| **侧表** | **9/12** | **10/12** | 10/12 | **0.336** | **674 ms** |

`ranks: [2, 6, 5, 3, 23, 5, 2, 4, 1, 0, 2, 3]`

**原因正是当初预判的那条**：主表的 VECTOR 索引里有 151,215 条完全相同的零向量，
HNSW 图被污染、召回下降；侧表只放 40,698 条真向量，图是干净的。
⇒ **「只存已嵌的行」不只是省空间，它是质量修复。**

最直观的一条：`Lyfe 四支柱是什么` 从**完全捞不到**变成第 **6**。

#### 执行记录（每步都验过）

```
create  建两张侧表                                    ✓
fill    191,913 行 tokenized / 40,698 行向量（约 10s）  ✓
index   FTS 走 INPLACE（快）；VECTOR 走 COPY（小表）    ✓
verify  行数对账 / 无空向量 / 两路可查                  ✓
enable  config 写入两张表名                            ✓
（此处先测检索，确认无回归）
slim    DROP 主表两列 —— **只用了 12 秒**              ✓
```

⚠️ **`slim` 比预期快两个数量级**：以为要重建 30–40 分钟，实际 12 秒。
原因是 `DROP COLUMN` 在 MariaDB 上是 INPLACE —— 只改表定义、不重写数据页。
这反过来说明：**当初「一次性建 8 个索引」的 50 分钟，全花在 VECTOR 的 COPY 上**，
与表的其余部分无关。

#### 一个坑：`index` 步被我自己的执行上限掐断

第一次跑 `index` 时被 120 秒上限杀掉，但**服务端那条 ALTER 继续跑**，
于是后续进程全都卡在 `Waiting for table metadata lock`。
⇒ 又验证了一次「断连 ≠ 取消」。长操作一律**后台子进程 + 轮询日志**。

#### 遗留

- `tools/rebuild_dsh_message.py` 已过时：它假定 `body_tokenized` / `embedding` 在主表上。
  下次整表重建要改成「主表 + 侧表」三段式。
- `dsh_message_old_20261107_091254`（3.7 GB）观察几天再删。
- memoria 那边的分表是**真正能发挥价值的地方**（13 GB 表），单独排期。
