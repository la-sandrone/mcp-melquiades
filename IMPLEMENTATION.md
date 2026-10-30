# 实施记录 v2.0

**日期**：2026-09-22
**依据**：`DESIGN.md` v0.1
**状态**：模块一/二/三已实现并通过实测；**未注册进 DSH**、未改 DSH 配置

---

## 一、交付物

### 新增

| 文件 | 职责 |
|---|---|
| `melquiades/config.py` | 配置加载（铁律三：魔法数字唯一出口）+ DSH_HOME 唯一口径解析 |
| `melquiades/dbcore.py` | **即弃连接**核心（铁律一）—— 无连接池 |
| `melquiades/schema.py` | 表结构演进（向量列/索引，幂等迁移） |
| `melquiades/tokenizer.py` | **copy 自 memoria-del-hielo**（铁律二）+ LIKE 转义/关键词切分 |
| `melquiades/retrieval.py` | 检索栈：五路 + 显式兜底链 |
| `melquiades/backfill.py` | 历史回填（tokenized / embedding） |
| `melquiades/cli.py` | CLI：ensure-tables / harvest / status / backfill |
| `melquiades/harvest/reader.py` | DSH 多帧 zstd 增量读 + 字节偏移游标 |
| `melquiades/harvest/extract.py` | compaction 事件 → 三层内容 |
| `melquiades/harvest/store.py` | 三张新表 + CAS 游标 + 幂等插入 + 日志 |
| `melquiades/harvest/runner.py` | 编排（发现→读→提取→入库→推进） |
| `melquiades/harvest/scheduler.py` | 定时线程（可急停）+ fire-and-forget |
| `DESIGN.md` | 设计稿 v0.1 |

### 重写

| 文件 | 变化 |
|---|---|
| `melquiades/server.py` | 7 工具（原 8 个）；去掉会话隔离/随机 key/审计/删除；新增游标读/批量追加/多路检索/tags 枚举/压缩档案 3 工具 |
| `config.yaml` | 去连接池；新增 search/dsh/draftpad 段；会话段改为说明 |
| `requirements.txt` | 新增 `zstandard==0.25.0` |

### 数据库新增（**未删任何现有表**）

| 表 | 说明 |
|---|---|
| `dsh_harvest_cursor` | 收割游标（`byte_offset`，非保留字 `offset`） |
| `dsh_compaction` | 压缩档案（L1 摘要 / L2 seq 指针 / L3 防漏网层 + 元数据） |
| `dsh_harvest_log` | 运行日志（DZ 老登方案；失败可查） |
| `draftpad.embedding` | 向量列 + `idx_vec_draftpad` |
| `dsh_compaction.embedding` | 向量列 + `idx_vec_compaction` |

---

## 二、实测验证结果

| 验证项 | 结果 |
|---|---|
| DSH 会话日志读取 | 单文件 **1833 帧** / 3,307 事件 / 0.58s |
| 增量游标幂等 | 从 `new_offset` 再读 → **0 帧 0 行** ✓ |
| 帧边界对齐行边界 | 80 帧全部以 `\n` 结尾 → 无需跨帧行缓冲 ✓ |
| compaction 提取 | 遮蔽 **719 条 / 426,131 tokens** —— 与既有 scan 工具报告**完全一致** ✓ |
| 全量首扫 | 61 文件 / 11,367 帧 / 42,818 事件 → **75 个压缩点**，零错误 |
| 三层入库 | 摘要 10,356 字 + L3 防漏网层 **69,542 字**（6.7×）+ 关键词 2,268 字 |
| 幂等插入 | 重复插入返回 False，行数不变 ✓ |
| CAS 游标 | `UPDATE ... WHERE byte_offset < ?` 推进成功 ✓ |
| 并发写同一行 | 800 次 × 4 方案实测（见 DESIGN §4.5） |
| 多路兜底链 | `chain=vector:0 → fulltext:0 → keyword(兜底):3` ✓ **降级可观测** |
| tags 枚举 | **3,589 种标签**（原先 0 次使用） |
| 全表向量扫 | 995 行 **4.1ms**（带零向量过滤） ✓ |
| 回填 tokenized | 944/944 成功，0 失败 |
| 回填 embedding | 见 `cli backfill status`（BGE :9006，1024 维） |

---

## 三、实现中按实测**修正**的判断（重要）

| # | 原判断 | 实测结果 | 处置 |
|---|---|---|---|
| 1 | 遮蔽 seq 取 `compaction/prune` | prune 只报**逐条增量**（实测 3 条）；`compaction/summary` **自带完整清单**（719 条） | 改以 summary 为主源，prune 作兜底 |
| 2 | 可加 `VECTOR(1024) NULL` 列 | `All parts of a VECTOR index must be NOT NULL` | 改 `NOT NULL DEFAULT (VEC_FromText('零向量'))`（实测对已有数据的表可行） |
| 3 | 零向量仅作占位、不影响检索 | 零向量行对真实查询的**距离为 0.0（排最前！）** | 查询必须 `embedding != VEC_FromText(zero)` 过滤 |
| 4 | 向量函数 `VEC_DISTANCE` | 不存在（"index is not found"）；正确名 `VEC_DISTANCE_COSINE` | 已改 |
| 5 | 嵌入端点 :9005 | 读取超时；:9006 可用（1024 维） | 多端点按序尝试 + 成功者记忆；config 把 9006 置首 |
| 6 | 列名 `offset` | **MariaDB 保留字**，建表语法错 | 改名 `byte_offset` |
| 7 | `deleted_at IS NULL` 通用 | `dsh_compaction` 无此列 | 软删列参数化（`deleted_col`） |
| 8 | mariadb 连接器有 `charset` 参数 | **没有**（1.1.14） | 改 `init_command="SET NAMES utf8mb4"` |
| 9 | draftpad 827 行 | 827 是 `table_rows` **估算**；实际 **995 行** | 文档更正 |
| 10 | 搭车扫描无副作用 | 进程退出时 `log_finish` 不执行 → **孤儿 `running` 记录** | 加节流（30s）+ 启动清理（标记 `interrupted`） |
| 11 | CJK 全文检索应走 jieba 分词列 `content_tokenized` | `innodb_ft_min_token_size=3` ⇒ **<3 字符的 token 不进索引**；jieba 把长串切成 2 字词（"口径"）**全被丢弃** ⇒ `MATCH(tokenized) '+口径*'` = **0**；而原始 `content` 列保留连续 4+ 字串 ⇒ **62 命中** | **全文路改用 `content` 列**（实测 0 → 3 命中）。jieba 分词在此反而有害 |

---

## 三·补 —— 真实失败案例调研（45 次失败 SQL）

数据源：3 场 DSH 会话的全部 `execute_sql` 调用（984 次），与 `tool/result` 配对取 `isError`。

| 失败类型 | 次数 | 根因 | v2 是否已解决 |
|---|---|---|---|
| **1406 Data too long**（`note_key`） | **30** | LLM 把整段标题（数百字）塞进 `VARCHAR(128)` | ✅ 工具层 `note_key[:128]` / `content[:max]` 截断 |
| **unexpected error** | 12 | LLM 用 `?` 占位符（官方 MCP 要 `%s`）；或把 `note_key` 当顶层参数传（pydantic 拒） | ✅ 工具签名固定，无占位符 |
| 1064 语法错误 | 2 | 内联字符串里的引号未转义 | ✅ 全参数化（`?`） |
| validation / 必填缺失 | 1 | 参数结构猜错 | ✅ 同上 |

**两条关键结论**：
1. **45 个失败 SQL 中 43 个含 `%` / `'` / `;` 等字符**（案例：`50%`、`Ω_Λ`、`O'Neill`、`Domènech`、`ΔT=3.3621mK`）
   ⇒ **"真实文本躲不开这些符号"得到实证**，故**只能转义 + 参数化，不能禁**。
2. **内联 31 次 vs 参数化 14 次**（失败样本内）⇒ 参数化确实更稳。
   **v2 的工具层恰好消除了上述绝大多数失败模式**——这是"封装 > 裸 SQL"的实证依据。

---

## 三·续 —— 安全加固（`sqlguard.py`）

**立场**（与用户对齐）：真正的防线是**绑定参数**；本模块只做**早失败 + 输入卫生**。

| 输入 | 性质 | 处置 |
|---|---|---|
| 值（`bucket` / `tags` / `query` / `condition_params`） | 数据 | **`?` 绑定参数**（+ LIKE 通配符 `escape_like`） |
| 表名 `table` | 标识符（无法参数化） | **白名单**（`draftpad` / `dsh_compaction`） |
| 列清单 `select_cols` | 结构（无法参数化） | **白名单**（预定义清单） |
| SQL 片段 `condition` | 结构（无法参数化） | **结构门禁**：禁 `;`/注释/空字节；禁 DML/DDL/DCL/子查询关键字；禁超长；**括号正序检查** |
| `limit` | 数值 | 夹紧到 `[1,500]` |

**明确不做**：**不禁止 `%` / `_` / `'` / `;` 出现在数据里**（真实学术文本必需）——它们该被**转义**或**参数化**，而不是被禁。

**本轮修掉的三个 bug**：

| # | 问题 | 现象 |
|---|---|---|
| A | `_keyword_search` **参数顺序颠倒**（SQL 里 `{where_extra}` 在前、`{clauses}` 在后，参数却传成 `(like…, session_id…)`） | **同时用 bucket/tags 过滤 + LIKE 时，明明有数据却 0 命中** |
| B | `condition` 只校验括号**总数**配平 | `1=1) OR (1=1` 总数配平，但首个 `)` 提前关闭包装括号 ⇒ `AND (1=1) OR (1=1)` ⇒ **OR 逃逸** |
| C | `select_cols` / `deleted_col` 靠调用方传 | 拿 draftpad 的列去查 dsh_compaction → `Unknown column` |

三个都改为**按表自动推断 / 正序括号扫描 / 参数顺序对齐**。

**实测验证**：

| 用例 | 结果 |
|---|---|
| 搜 `50%` | 只命中字面含 `50%`；**不误命中** `50 percent` ✅ |
| 搜 `a_b` | 只命中字面 `a_b`；**不误伤** `axb` ✅ |
| 搜 `c:\tmp` / `O'Neill` / `Ω_Λ` | 字面匹配正确 ✅ |
| 注入 4 种（`'; DROP TABLE…` 等） | 全部无异常、表安全（参数化兜底）✅ |
| 危险 condition 9 种（多语句/注释/子查询/括号逃逸/超长） | **9/9 拦截**，工具层返回 `REJECTED` ✅ |

---

## 三·续·2 —— 安全加固升级为「三层防线」（2026-09-22 晚）

**起因**：用户指出（一）"任何输入都该过滤敏感词并转义"（二）"重点是**转义**，`%` 之类难避免"
（三）"直觉应该有**现成饭库**"。三条都成立，于是从「手写关键字黑名单」升级为三层：

| 层 | 手段 | 强度 | 说明 |
|---|---|---|---|
| **1. 参数化** | `?` 绑定参数 + `escape_like` | 主防线 | 所有**值**走这里；LIKE 的 `%`/`_`/`\` 一律转义 |
| **2. 只读会话** | `SET SESSION TRANSACTION READ ONLY` | **最硬** | 所有检索路径启用。实测 `CREATE/DROP/INSERT/UPDATE/DELETE` 全部被服务器拒绝：`Cannot execute statement in a READ ONLY transaction` |
| **3. AST 白名单** | **`sqlglot`**（30.18.0）解析 `condition` | 精确 | 见下 |

**为什么换掉手写黑名单**：黑名单漏一个关键字就完蛋；白名单 + 真解析器是"**只放行我能理解的语法**"。

**`sqlglot` 实测能力**：

| 危险输入 | 检出方式 |
|---|---|
| `1=1; DROP TABLE draftpad` | 解析成 **2 条语句** ⇒ 拒绝 |
| `AND (SELECT 1)` / `EXISTS(SELECT…)` | `Subquery` / `Select` 节点 ⇒ 拒绝 |
| `DELETE FROM draftpad` / `EXISTS(…FROM mysql.user)` | `Table` 节点 ⇒ 拒绝 |
| `content = (SELECT 1 INTO OUTFILE …)` | **解析直接失败** ⇒ 拒绝 |
| `content LIKE 'x' OR SLEEP(5)` | `SLEEP` 不在节点白名单 ⇒ 拒绝 |

合法条件（`LIKE` / `IN` / `CHAR_LENGTH(…) > n` / `REGEXP` / `?` 占位符）只用到
`Column / Identifier / Literal / Placeholder / Like / In / Or / GT / Length / RegexpLike` 等节点 ⇒ 白名单可行。

**两个 sqlglot 的能力边界（实测发现，必须自己补）**：
1. **看不到注释** —— `/*c*/`、`-- x` 在 tokenize/parse 阶段就被丢弃，AST 与 token 层都无痕。
   而注释留在原样片段里会**吃掉外层拼的 `)` 与 `ORDER BY … LIMIT ?`**
   ⇒ 自实现 `_has_comment()`：**跳过字符串字面量**后扫描（`LIKE '%--%'` 不误伤）。
2. **括号逃逸需自查** —— `1=1) OR (1=1` 会被 sqlglot 判为解析失败（好事），
   但仍保留 `_balanced_parens()` 正序扫描（任何前缀不得出现未配对的 `)`）。

**本轮修掉的问题**：

| # | 问题 | 现象 |
|---|---|---|
| D | AST 白名单漏了 `Placeholder` | `content LIKE ?`（**condition_params 的核心用法**）被误拒 |
| E | 注释检测依赖 sqlglot tokenizer，异常被 `except: pass` 吞掉 | **注释形同虚设**（我自己踩了"静默降级"）→ 改为自实现扫描 |

**⚠️ 遗留的权限问题（建议处置，未执行）**：

`melquiades` 用户当前对 `melquiades_medicamento` 持有：
```
SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER, CREATE VIEW
```
**它能 `DROP TABLE`**。三层防线已让已知路径不可达，但"有能力删表"本身不该存在。
**建议**：另建只读用户专供检索（`SELECT` only on `melquiades_medicamento`），写路径才用现有用户。
`setup_mariadb_user.sql` 需要同步更新。

**最终实测**：合法 condition **8/8 通过**；危险 condition **14/14 拦截**；
LIKE 转义 5/5（`50%` 不误命中 `50 percent`、`a_b` 不误伤 `axb`、`O'Neill`/`Ω_Λ` 正确）；
注入 4 种无异常；检索三路回归正常。

---

## 三·续·3 —— 排查与修复：8 项缺陷（2026-09-22 深夜）

**触发方式**：为一场长会话生成对话索引时，按 System Prompt 的「主动回查」原则先去
查自己的压缩档案 —— 当场撞出 8 个问题，其中 **4 个属「静默降级」**（表现为
"看起来正常、结果却是错的或空的"）。

| # | 问题 | 根因 | 修法 |
|---|---|---|---|
| P11a/b/c | **零向量污染**（最严重）：零向量混入语义检索，因 `dist=0` 排最前并拿假最高分 `1/(1+0)=1.0` | ① `insert_compaction` 不写 `embedding` ⇒ 新行落 `DEFAULT` 零向量；② 排除条件 `embedding != VEC_FromText(?)` 在 **VECTOR 类型上实测无效**；③ 同因使 `backfill` 的 `= VEC_FromText(?)` 找不到缺失行（29/103 就是如此积下的） | 新增 **`embedded` 标记列**（`schema.ensure_embedded_column`），"是否已嵌入"一律以它为准；迁移 + 重嵌 35 行；收割后自动补嵌（`runner.embed_pending`） |
| P10 | 全文路**静默降级**：`counts=fulltext:0` 装成"无命中" | `MATCH(content)` 对 `dsh_compaction` 必然报 `Unknown column`（该表正文是 `summary`），异常被 `except` 吞成 `logger.warning` | 按表选列 + 抛 `FulltextPathError`，由 `find()` 记入 `meta.notes` |
| P2 | `condition` 在非 `mode=condition` 下**被静默丢弃** | 只有 `MODE_CONDITION` 分支把它拼进 SQL | `query` 空 ⇒ 自动切 condition 模式；`query` 非空 ⇒ 并入附加 `WHERE`（均在 notes 说明） |
| P13 | 单路模式下 `score` 恒为 `0.5/0.25/0.1667`，无法判断"像不像" | `_merge` 用 `weight/(1+rank)` 覆盖了本路真实相似度 | 新增 `Hit.raw_score` 保留原始相似度；`score` 仍作融合排序分 |
| P1 | 输出 `bucket=<uuid> 类型= tags=-` 全是噪声 | `_fmt_hits` 只有一套标签（草稿纸语义），硬套到 compaction 表 | 按表分两套标签，并带出 `turn_no/shadowed_count/shadowed_tokens` |
| P3/P4 | 拿 `$DSH_SESSION_ID` 查库**必 0 命中** | 环境变量是 `session-<uuid>`，而库里 `dsh_session` 存**裸 uuid**（`harvest/reader.py` 有意剥前缀） | `archive_find` 加 `session="current"`（服务端自动剥前缀） |
| P14 | 全文路 0 命中易被误读成"库里没有" | 未提示 `innodb_ft_min_token_size=3`（2 字中文词不进索引） | notes 里显式提示改用 keyword 路 |
| P12 | `summary_excerpts` 多数为空且**静默不显示** | `build_excerpts` 只抽"落在本收割批次内"的被遮蔽事件（有意设计，回查由 L2 seq 指针承担） | `archive_expand` 空时显式说明原因 |

**顺带重构 / 卫生**

- SELECT 列清单提为 `sqlguard` 的**唯一真相源**（`COLS_DRAFTPAD` / `COLS_COMPACTION` /
  `COLS_FULL`）：`guard_select_cols` 是**整串**白名单匹配，留多个副本必然漂移。
- 新增 `.gitignore`（`__pycache__` 等），并把已追踪的 `.pyc` 移出索引。

**验证**：`tools/verify_archive_v2.py`（直连新代码，绕开仍是旧代码的 MCP 进程）
—— embedded 100% 已嵌 / 向量路 `sim` 有区分度 / 全文路可用 / condition 自动切换与并入 /
量级字段带出 / `draftpad` 侧无回归。

**⚠️ 生效条件**：MCP 进程加载的仍是旧代码，**重启 dsh 会话**后新逻辑才在工具侧生效。

**教训**：这 8 个问题全是"主动回查"逼出来的 —— 若当时凭压缩摘要直接编对话索引，
一个都不会暴露。**摘要给线索，原文给真相。**

---

## 四、用法

```bash
cd <project-root>
export PYTHONPATH=.

# 建表 + 向量列（幂等）
~/ai-services/bin/python -m melquiades.cli ensure-tables

# 首扫（全量，手动跑一次；**不进 MCP 启动路径**）
~/ai-services/bin/python -m melquiades.cli harvest --backfill

# 单轮增量
~/ai-services/bin/python -m melquiades.cli harvest

# 状态（游标 / 档案数 / 最近收割）
~/ai-services/bin/python -m melquiades.cli status --logs 5

# 回填历史（幂等，可断点续跑）
~/ai-services/bin/python -m melquiades.cli backfill all
~/ai-services/bin/python -m melquiades.cli backfill embedding --table dsh_compaction
```

MCP 工具（7 个）：
`draftpad_write` / `draftpad_scan` / `draftpad_amend` / `draftpad_find` / `draftpad_tags`
`archive_find` / `archive_expand` / `archive_status`

---

## 五、三触发的落地形态

| 触发 | 实现 | 状态 |
|---|---|---|
| A. 定时器 | `HarvestScheduler`（daemon 线程 + `threading.Event` 可急停），间隔 60s | ✅ 随 `server.main()` 启动 |
| B. 搭车 | `_piggyback()` = fire-and-forget，**带 30s 节流** | ✅ 各工具调用后触发 |
| C. 启动扫 | `spawn_fire_and_forget(runner.startup_scan)`，**只做增量**、限 10 个文件 | ✅ 随 `_init_state` 启动 |

**首次全量扫不在启动路径**（170MB 会拖住 initialize）—— 由 CLI `--backfill` 手动跑，已执行。

---

## 六、遗留 / 待办

| # | 事项 | 说明 |
|---|---|---|
| 1 | **未注册进 DSH** | 按约定不动 DSH preset（避免 duplicate entry id 风险）。需要时由用户决定 |
| 2 | `mcp_session_recall` 可退役 | 其三项能力（字面检索/按 seq 取原文/压缩点清单）已由 `archive_*` 接替**并实测**；System Prompt 的 recall 指引也已换成 `archive_*` 三段式（见 #9），可随时退役 |
| 3 | ~~`dsh_compaction` 的向量回填~~ **已完成** | 2026-09-22：迁移 `embedded` 列 + 重嵌 35 行 + 收割后自动补嵌，详见「三·续·3」 |
| 4 | context rot 曲线 | 数据已齐（`dsh_harvest_log` + `dsh_compaction` 的 tokens/时间），分析脚本未写 |
| 5 | `scratch_notes` 改名 | 已定"保留原地 + 改名标明非生产"（建议 `misc_notes`），**尚未执行** |
| 6 | `README.md` 重写 | 已定"落实代码时改"，本次未改 |
| 7 | `db.py` 去池化 | **已完成（2026-10-30）**：新代码走 `dbcore.py`（即弃连接）；旧 `db.py` 及只消费它的 `search.py` 已**整体删除**（均无任何 import、无动态加载、README 目录树与 DESIGN 决策记录已同步） |
| 8 | `draftpad_vectors` 空表 | 保留未用（已拍板） |
| 9 | **System Prompt 同步**（已完成） | 5 个 `.txt` 源文件（UTF-16-LE）+ 5 个 preset 的「主动回忆」段换成 `archive_*` 四段式，并在**四处**强调"摘要是有损的（约 35:1），命中压缩点后必须回查原文"；`tools/apply_prompt_v6.py` 幂等可重跑，备份 `*.bak-archive-20260922-1646` |
