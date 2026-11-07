# mcp-melquiades

**DSH 会话的草稿纸 + 压缩档案回查 —— 一个由 MariaDB 支撑的 MCP 服务器。**

名字取自加西亚·马尔克斯《百年孤独》里的**墨尔基阿德斯**（Melquíades）——那个把布恩迪亚家族
百年史写在羊皮卷上、并且「**没有按人类惯常的时间顺序来排列这些事件**」的人。本项目与同门的
[mcp-pergaminos]、[memoria-del-hielo] 共享这一母题：**身份稳定，顺序易变**。

这句话在这里不是修辞，是设计约束：**DSH 的会话日志会改名、换代、重编号**，而档案指针不能
跟着碎掉。本项目为此付出的大部分工程努力，都在这一条上（见 §4）。


> ## ⚠️ 给所有读者（不管你是 LLM 还是人类）
>
> **这个项目处理的数据会疯狂拷打 Zipf 定律和 Unicode 的边缘情况。**
>
> 语料里同时有：中文（无空格分词、单字也要能搜）、拉丁变体（`é ü ñ`、古典长音
> `ā ă ē ĕ`）、希腊文（含词尾 sigma `ς`/`σ`，`lower()` 不合并）、西里尔文
> （`Византийская библиотека`）、古斯拉夫语古字母（`ѣ ѫ ѳ`），以及大量只出现
> 一两次的专名与自造词——**Zipf 长尾的尾巴比一般语料重得多**。
>
> 由此产生的坑有个共同特征：**它们不报错**。缺 `re.UNICODE` 只是少匹配；
> NFC/NFD 不一致只是查不到；按码点截断只是显示乱码；`min_token_size=3`
> 只是让两字中文词进不了索引。**全都「能跑」，错误是沉默的、延迟发作的、
> 只有拿真实材料去喂才会暴露的。**
>
> 所以，改动任何**分词 / 索引 / 匹配 / 截断**逻辑之前，先问自己一句：
>
> **「这段代码假设了文本长什么样？这个假设在俄文、希腊文、变音拉丁文、
> 以及只出现一次的自造词上还成立吗？」**
>
> 参考实例（都是真踩过的）：
> - `[A-Za-z0-9_]+` 当"非 CJK 分支" → 俄希拉丁整段被丢，tokenized 列为空
> - `innodb_ft_min_token_size = 3`（默认）→ 中文两字词全丢，FULLTEXT 返回 0 而 LIKE 能中
> - jieba 按词典切分 → 自造词/专业术语切错边界，约 30% 漏搜
> - 按码点切块 → emoji ZWJ 序列（`👨‍👩‍👧‍👦` 是 7 个码点）、天城文连字被切成两半
>
> 切分的最小正确单位是**字素簇**（UAX #29，Python 里用 `regex` 的 `\X`），
> 不是字节、不是码点、也不是"看起来像几个方块"。


## ⚠️ 数据量已经不是玩具了 —— 请严肃对待性能

现有规模：`dsh_message` **约 14 万行 / 2.4 GB**（含 `MEDIUMTEXT` 正文、CJK bigram 倒排索引、1024 维向量列），`draftpad` 1,800 行，`dsh_compaction` 181 行。
别被「行数不多」骗了——**行宽很大**（正文 + tokenized + 向量），全表扫一次就是 2.4 GB。

**任何涉及全表操作的设计决定，先算成本再动手。**

### MariaDB 与 MySQL 的几处微妙区别（都是真踩过的）

网上多数 FULLTEXT / DDL 的"最佳实践"是照着 MySQL 写的，**照搬到 MariaDB 会出事**：

| 事项 | MariaDB 的行为 | MySQL 的行为 | 后果 |
|---|---|---|---|
| `ALTER TABLE ... ALGORITHM=INPLACE` 对 FULLTEXT 索引 | **仍然 rebuild 整表**（INPLACE 只省锁，不省 IO） | `DROP INDEX` 是 In Place，不重建 | "摘索引 → 改数据 → 建索引"在 MariaDB 上要**复制表两趟**（实测 91 min + 82 min） |
| 同一条语句里 `DROP INDEX a, ADD FULLTEXT a(...)`（定义相同） | **被优化成空操作**：0 秒完成且**没有任何效果** | — | 必须拆成两条独立语句才对 |
| `SET GLOBAL innodb_ft_min_token_size` | **静默成功但完全不生效**（startup-only） | 同类行为 | 必须改 `my.ini` + 重启服务 |
| `innodb_ft_min_token_size` 的计数单位 | **字符**（不是字节） | 也按字符 | 默认 3 ⇒ **所有 1–2 字中文词进不了索引**，FULLTEXT 返回 0 而 LIKE 能命中 |
| `VECTOR` 类型 / `VEC_DISTANCE_COSINE` / `VECTOR KEY` | 11.7+ **原生支持** | **没有** | 迁到 MySQL 得换第三方向量扩展；`VEC_DISTANCE` 会报 "index is not found"，必须写 `VEC_DISTANCE_COSINE` |
| 改全局变量所需权限 | `SUPER` | 8.0 起用 `SYSTEM_VARIABLES_ADMIN` | 业务账号 `SET GLOBAL` 会 `ERROR 1227 Access denied` |
| `information_schema.innodb_buffer_pool_pages` | **不存在** | 存在 | 查缓冲池状态得换路径（本项目已踩） |
| **`MODIFY COLUMN`（哪怕只改 `COMMENT`）在带 FULLTEXT 的表上** | **只能 `ALGORITHM=COPY`（重建整表）**：`INSTANT`/`NOCOPY` 都被拒，Reason 是 `InnoDB presently supports one FULLTEXT index creation at a time` | 无 FULLTEXT 时 `INSTANT` 可行（实测 **25.9 ms**） | **改注释别单独做**——攒到下次必须重建（改列类型/加索引）时合并进同一条 ALTER |
| `GRANT ... ON db_name.*` 里的 `_` | 被当**通配符**（与 `%` 同） | 同 | 授权后会多出一行"未转义"的 `mysql.db` 记录；两行并存时**只命中其中一行**，导致 GRANT 加了却不生效（实测踩过） |

### 性能纪律

1. **先看量级**：行数 × 行宽 = 要搬多少字节。14 万行 × 17 KB/行 ≈ 2.4 GB，
   在内存里扫一遍约 385 ms（6 GB/s 量级）；乘以批次数就知道能不能接受。
2. **再问索引**：`EXPLAIN` 一次的成本远低于猜。缺索引的 UPDATE 会因全表扫
   从 1 ms 变 385 ms（本项目实测，**188 倍**）。
3. **FULLTEXT 的 DDL 按小时算**：MariaDB 上它是 rebuild 整表，不是加个 B 树。
   大表（>10 GB）预留 1–2 小时，并且**别和别的重 IO 任务同时跑**。
4. **影子表切换**：建新表 → 搬数据 → 建索引 → RENAME → **立刻删旧表**
   （否则白占磁盘，还污染 buffer pool 的 LRU）。
5. **缓冲池要装得下热数据**：数据量超过 `innodb_buffer_pool_size` 后，
   命中率会断崖式下降。
6. **失败要重试、断点要续传**：这类长任务必须能被打断（Windows 自动更新
   随时可能杀掉你的后台进程），设计时就要有幂等 + 水位线。

> 完整的两次误判与 188 倍修复过程见工作区笔记
> `用户笔记/检索/等待归因实验-dsh_message嵌入写回.md`。


### 向量检索是**近似**的（HNSW / ANN）—— 别拿它做精确查找

MariaDB 的 `VECTOR KEY` 是 **HNSW（近似最近邻）**，不是精确 KNN。
**recall 可以低于 100%，而且它不会告诉你漏了什么。**

2026-10-25 在 `documents_book_chunk`（**62.5 万点**）上用「拿一条 chunk 的向量查它自己」实测：

| 指标 | 结果 |
|---|---|
| 自查询 **top-1 命中自己** | 12/13 = **92%** |
| 自己在 **top-10** 里 | 13/13 = **100%** |
| `mhnsw_ef_search` 20 → 400 | **毫无变化**（MariaDB 12.3.2 上该参数未生效） |

**排除过程**（三步，值得记）：

| 测试 | 结果 | 结论 |
|---|---|---|
| `IGNORE INDEX (idx_vec_chunk)` 暴力扫描 | ✓ 自己排第一，距离 −0.000000 | 数据没问题 |
| Python 手算余弦 `1 − a·b/(‖a‖‖b‖)` | ✓ 0.000000000 | 距离函数没问题 |
| `EXPLAIN` | `type=index, key=idx_vec_chunk`，无 filesort | 确实走了索引 |

⇒ **数据对、算得对、计划对 —— 是索引给了近似答案。**

### 两条纪律

1. ✅ **可以用它做语义召回**（top-k）。实测 top-10 recall 100%，
   「找意思相近的段落」完全够用。
2. ❌ **绝不要用它做精确查找**——自查询、判重、找重复、验证「某条是否已入库」。
   那会撞上那 8%。精确查找走 **FULLTEXT 或 LIKE**。

表越小 recall 越高（图小、连通性好），但**不要假设小表就是 100%**。

> 另注：`VEC_DISTANCE`（不带 `_COSINE`）会报 `index is not found`；
> 建索引时 `DISTANCE=cosine` 必须显式写，否则静默降级为
> `mhnsw_default_distance`（euclidean）。这两个坑都踩过。

> ⚠️ **丑话说在前头**：这是个人满足自己需求的娱乐作品。它为「跨会话找回刚被压缩掉的东西」
> 而写，绑定了 [DeepSeek Harness]（下称 **DSH**）的**私有**会话日志格式。**如果你不用 DSH，
> 它对你基本没用。** 作者不承诺接口稳定，但承诺**不静默降级**——上游一变，它会吵，不会装死。

---

## 1. 它做什么

三个能力，一个 MCP 服务器：

| 能力 | 说明 |
|---|---|
| **草稿纸**（draftpad） | 跨对话的项目账本。原子事实 / 数字 / 中间结论，写给**未来的自己**（或下一个 LLM）。可 `write` 追加、可 `amend` 修订（追加式，不改原文）、可 `find` 检索。 |
| **压缩档案回查**（archive） | 离线**收割** DSH 的自动压缩产物（摘要 + 被遮蔽事件的指针），入库后可检索；命中后能**回到原文**按 `seq` 或 `guid` 取回完整未删节正文。 |
| **检索栈** | 多路检索（向量 + 全文 + 线索路…）＋ RRF 融合 ＋ 跨编码器精排 ＋ 受控词表（同义词环）查询扩展，带**显式兜底链**与**不确定性提示**。 |

**它不做什么**：不写入 DSH、不修改任何会话日志、不进 DSH 进程。收割是**只读**的。
（这是有意的：侵入上游进程的代价，远大于"压缩后产物"本身的价值。）

### 1.1 为什么需要它

DSH 会自动压缩长会话——把早期消息压成一段约 35:1 的摘要。摘要是有损的：
**没被写进摘要的词，在摘要里永远搜不到**。而真实的痛点是
「**开新对话找不到之前的东西**」、以及「**刚被压掉的内容还想要**」。

本项目把压缩产物连同**被遮蔽事件的身份锚**一起入库，使"刚被压掉的内容"可以被检索、
并且能**回到原文**——即使原文所在的日志文件已经改名、换代、甚至消失。

---

## 2. 工具清单（10 个）

| 工具 | 用途 |
|---|---|
| `draftpad_write` | 追加一条笔记（推荐） |
| `draftpad_scan` | 按游标顺序读账本（省 token 的浏览） |
| `draftpad_amend` | **批量追加式修订**（不改原文，在末尾追加指针段） |
| `draftpad_find` | 多路检索（精确 `auto` / 模糊 `vector` / 纯字面 `fulltext`…）|
| `draftpad_tags` | 枚举用过的标签 |
| `archive_find` | 检索压缩档案（摘要 + 防漏网层） |
| `archive_expand` | 展开一条档案：摘要全文 + 遮蔽指针清单 |
| `archive_fetch` | **取回被遮蔽事件的原文**（seq / guid 双入口） |
| `archive_grep` | 在**某个压缩点**的遮蔽区内按关键词搜原文 |
| `archive_status` | 收割器与档案的健康检查 |

### 2.1 HTTP 桥（可选，与 MCP 同生共死）

模型能调上面这些工具，**人不能**。要给 Web UI 一个入口，就得把检索暴露成 HTTP。

桥是 **MCP 进程内的 daemon 线程**，不是独立进程：

```
GET  /health   → {ok, db, sources, modes}
POST /search   → {ok, hits, stats, notes, took_ms}
POST /context  → {ok, text}
```

- **同生共死**：MCP 起来桥就在，MCP 死桥就没了 → 不留孤儿进程占端口
- **端口被占只告警不致命**：MCP 是本命，桥是附件
- **只读**：没有任何写入端点；只绑回环并校验 Host（防 DNS rebinding）
- 关掉：`config.yaml` 里 `bridge: { enabled: false }`

**端口怎么对上**：桥启动时把事实写到 `~/.melquiades/bridge.json`（退出时删），
DSH 侧插件读它而不是猜——配置里写错端口也不会连错。失败原因也写在同一文件里，
所以「为什么连不上」不需要翻日志。

配套的 DSH 侧插件在 `plugins/dsh-omnibus-search`（侧边栏「统一检索」tab）。

> 也可以脱离 MCP 单独跑（调试用）：`melquiades-http --port 9010`。
> 但两种模式**不要同时开**——会抢端口，后起的那个只告警不启动。

---

## 3. 安装

**核心（MCP 服务器本体：轻量，无需 CUDA / torch）**

```sh
pip install -e .
# 或不装包，直接用：python -m melquiades.cli / python -m melquiades.server
```

核心依赖：`mcp`、`mariadb`、`PyYAML`、`httpx`、`jieba`、`sqlglot`、`zstandard`、`rapidfuzz`。
其中 `jieba` / `sqlglot` / `zstandard` 缺失时**各自降级**（CJK 全文 / AST 门禁 / 收割器），
不影响其余功能——降级都会显式告警，不静默。

**可选：自建嵌入代理**（`[embed]` extra）——只有你要跑向量检索才需要，含 CUDA torch：

```sh
# 先装 CUDA 版 torch（版本必须三者对齐：torch / torchaudio / torchvision 同 minor）
pip install torch==2.10.0+cu126 torchvision==0.25.0+cu126 torchaudio==2.10.0+cu126 \
    --index-url https://download.pytorch.org/whl/cu126
pip install -e '.[embed]'
```

> `torch` 全家桶**必须显式锁版本**：`torchaudio` 与 `torch` 差一个 minor，
> 就会在 `undefined symbol` 上炸——而这条错配能潜伏半年不发作，
> 直到 `sentence-transformers → transformers → loss_rnnt → torchaudio` 这条
> 死亡 import 路径被走通。详见 `NOTES.md`。

**可选：预下载模型权重**（`[models]` extra）：`pip install -e '.[models]' && python download_models.py`。

> **嵌入代理（`server_bge.py`）不在本仓库。** 它是同门项目
> [memoria-del-hielo] 里的一个双端口 BGE 嵌入 / 重排代理（9005 本地优先 / 9006 远端优先），
> 本项目默认指向它。**没有嵌入代理时，向量路与精排路不可用，全文 / LIKE 路照常工作。**

---

## 4. 与 DSH 的关系 —— 上游适配（本项目的核心工程）

### 4.1 耦合点（每一处都有实测出处，不是推测）

| # | 位置 | 依赖的上游事实 | 上游变化时的**症状** | 现有防线 |
|---|---|---|---|---|
| 1 | `harvest/runner.discover_logs` | 日志名 `session.v<N>.jsonl.zstd`；目录布局 | 新代际**发现不了** → 收割静默归零 | ✅ **按名解析代数、取代际最大**（v5/v6… 无需改码） |
| 2 | `harvest/reader` | jsonl + **多帧 zstd** 追加流；字节偏移游标 | 解压 / 切分失败 | ✅ 显式报错 |
| 3 | `harvest/extract`、`harvest/message_extract` | compaction 事件结构与消息正文路径 | 抄录 0 条 | ✅ `harvest/drift` 漂移探测器（§4.2） |
| 4 | `harvest/compaction_v2` | `guid = sha256(session_id‖gens‖line_no‖block_path)`；V3→V4 **会重编号 seq** | 指针错位 / 回查失效 | ✅ guid **与 seq 脱钩**（对重编号免疫） |
| 5 | `config.resolve_dsh_home` | `DSH_VERSION` 文件；home 名规则 `dsh-domus-<版本>` | home 找不到 → **静默归零** | ✅ **引真相源派生**（不抄结果） |
| 6 | `model.SESSION_ID_LENGTH`；`message_runner` | session_id 长度 / 前缀口径 | 取不到 session_id | ✅ 三级兜底链 |

**架构纪律**：**所有 DSH 格式知识只许出现在 `melquiades/harvest/` 与
`config.resolve_dsh_home`。** 改上游适配就改这两处；别把格式知识漏进 `retrieval.py` 之类的地方。

### 4.2 上游漂移探测器

`melquiades/harvest/drift.py` 持有 DSH 事件类型的**实测基线**（对 40 个真实会话日志做直方图，
得 38 种顶层类型）。它报两类事实：

- `unknown_event_types(events)` —— **多出来的**类型（升级会新增，改名才是事故）；
- `missing_content_types(events)` —— **少掉的、本该承载正文的**类型（改名 = 静默少收的前兆）。

> **升级 DSH 之后的第一件事**：跑 `melquiades status`（已内置这项检查，默认扫最近 1 个日志，
> `--drift-logs N` 可加宽）。看有没有未登记的类型——比"等线上发现内容变少"便宜得多。

### 4.3 兼容性

| melquiades | 已验证的 DSH | 备注 |
|---|---|---|
| `0.1.x` | `0.1.7-rc.2` | 会话日志格式 V3 / V4 并存已适配 |

（作者会随 DSH 升级更新此表；release notes 里会写明适配的 DSH 版本。）

### 4.4 适配流程（固定闭环，每次都一样）

```
① 升级 DSH
② pytest                              → 哪条契约破了 / 有没有破
③ 喂新日志给 harvest/drift            → 有没有未登记 / 消失的事件类型
④ 只改隔离层（harvest/ 或 config.resolve_dsh_home）
⑤ 补一条黄金样本 fixture（把这次的上游形态钉住）
⑥ 更新本表 + 版本号 → 重新导出发布件（§8）
```

---

## 5. 配置

```sh
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

`config.yaml` **含数据库口令，不要提交**（`.gitignore` 已覆盖它）。
配置的每一项都**有处可查**：所有魔法数字（超时、阈值、权重、池大小）都在
`config.yaml` 的注释里写明了取值依据与实测数据——**改它之前先读那段注释**。

关键项：

```yaml
mariadb:
  host: 127.0.0.1
  port: 3306
  database: melquiades_medicamento
  user: melquiades
  password: "……"                  # 必填

dsh:
  harness_dir: /path/to/deepseek-harness   # home 名由 <此>/DSH_VERSION 派生
  home: ""                                  # 留空 = 派生；填值则覆盖（逃生口）

search:
  bge_endpoints: ["http://localhost:9006", "http://localhost:9005"]
  bge_model: "remote:BAAI/bge-m3"           # `remote:` 前缀必留（见 config 注释）
```

数据库与低权限用户需要先建好（示例，口令自定）：

```sql
CREATE DATABASE IF NOT EXISTS melquiades_medicamento
  CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci;
CREATE USER IF NOT EXISTS 'melquiades'@'localhost' IDENTIFIED BY '<你的口令>';
GRANT ALL PRIVILEGES ON melquiades_medicamento.* TO 'melquiades'@'localhost';
```

---

## 6. 运行

**初始化数据库表**（幂等）：

```sh
python -m melquiades.cli ensure-tables
```

**首扫历史日志**（一次性；170MB 量级，**不进 MCP 启动路径**）：

```sh
python -m melquiades.cli harvest --backfill
```

**查看状态**（含**上游漂移扫描**：默认扫最近 1 个日志的事件类型，未登记即告警）：

```sh
python -m melquiades.cli status --logs 10
python -m melquiades.cli status --drift-logs 5     # 加宽到 5 个日志
```

**作为 MCP 服务器（stdio）**：

```sh
python -m melquiades.server
```

MCP 客户端（如 DSH）配置示例：

```yaml
- name: melquiades
  command: bash
  args: ["-l", "/path/to/mcp-stdio.sh", "melquiades"]
```

---

## 7. 测试

```sh
pip install -e '.[test]'
pytest
```

测试分两层：

1. **纯单元**：`tokenizer`（jieba / CJK 边界）、`sqlguard`（condition 的 AST 白名单）、
   `compaction_v2`（代数解析 / 44 字符契约）、`vocab`（同义词环）、`config`（home 派生）。
2. **黄金样本**：`tests/fixtures/dsh_session_golden.jsonl` 是一份**合成**的 DSH 事件样本
   （真实日志是个人对话内容，进仓即泄露，故不进仓；测试时现场把它编成**多帧 zstd**、
   摆成 DSH 的目录布局），跑通 `发现日志 → 增量读 → 事件解析 → 抽取压缩记录` 全链路。

> 这套测试是**上游适配的回归网**：DSH 一变，`pytest` 先响，
> 而不是等线上静默归零（本项目历史上吃过两次这种亏：home 硬编码、日志名写死）。

---

## 8. 发布件怎么生成

```sh
MELQUIADES_PUBLIC_DST=/path/to/public-dir ./tools/export_to_public.sh
```

**白名单导出 + 泄露闸门**（不是 `cp -r`）：白名单之外的东西永远不会被带出去，
且每次导出都是一次泄露检测——凭据 / 已知机密 / 机器绝对路径 / 真实邮箱 / 必备件 / 纯净度 /
语法，任一命中即**拒绝**（`exit 1`），不静默放行。

脱敏规则在 `.publish-redactions`、已知机密登记在 `.publish-secrets`——两者都**不发布**。
脱敏**必须覆盖闸门**：闸门扫的是脱敏后的导出件，规则漏一条就当场 FAIL。

---

## 9. 目录结构

```
melquiades/
├── melquiades/                # 包本体
│   ├── server.py              # MCP 入口（10 个工具）
│   ├── cli.py                 # ensure-tables / harvest / status / backfill
│   ├── config.py              # 配置（魔法数字唯一出口）+ DSH home 解析
│   ├── dbcore.py / schema.py                  # 即弃连接 + 表结构演进
│   ├── retrieval.py / rerank.py               # 检索栈
│   ├── tokenizer.py / vocab.py / sqlguard.py  # 分词 / 词表 / SQL 门禁
│   └── harvest/               # ← 所有 DSH 格式知识都在这
│       ├── reader.py          #   多帧 zstd 增量读 + 字节偏移游标
│       ├── extract.py         #   compaction 事件 → 三层内容
│       ├── message_extract.py #   消息 / 附件抽取
│       ├── compaction_v2.py   #   seq→guid 译法（唯一真相源）
│       ├── drift.py           #   ★ 上游漂移探测器
│       └── runner.py / store.py / scheduler.py
├── tests/                     # 单元 + 黄金样本
├── tools/                     # export_to_public.sh / scan_paths.py / 回填 / 校验
├── config.example.yaml
└── pyproject.toml
```

---

## 10. 许可

**代码**：AGPL-3.0-only（见 `LICENSE`）。

本项目**只读** DSH 的会话日志、**不修改**、**不代持**。它存储的是**你自己**的会话内容的
一份衍生副本（摘要 + 指针 + 你主动写入的笔记）——这些内容归你，怎么用由你决定。

---

[mcp-pergaminos]: https://github.com/la-sandrone/mcp-pergaminos
[memoria-del-hielo]: https://github.com/la-sandrone/memoria-del-hielo
[DeepSeek Harness]: https://github.com/deepseek-ai
