# mcp-melquiades

**DSH 会话的草稿纸 + 压缩档案回查 —— 一个由 MariaDB 支撑的 MCP 服务器。**

名字取自加西亚·马尔克斯《百年孤独》里的**墨尔基阿德斯**（Melquíades）——那个把布恩迪亚家族
百年史写在羊皮卷上、并且「**没有按人类惯常的时间顺序来排列这些事件**」的人。本项目与同门的
[mcp-pergaminos]、[memoria-del-hielo] 共享这一母题：**身份稳定，顺序易变**。

这句话在这里不是修辞，是设计约束：**DSH 的会话日志会改名、换代、重编号**，而档案指针不能
跟着碎掉。本项目为此付出的大部分工程努力，都在这一条上（见 §4）。

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
│   ├── dbcore.py / db.py / schema.py          # 即弃连接 + 表结构演进
│   ├── retrieval.py / search.py / rerank.py   # 检索栈
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
