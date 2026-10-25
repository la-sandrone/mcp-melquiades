# CHANGELOG

格式：按日期倒序。每条记「**为什么**」而不只是「改了什么」——因为半年后回来看，
动机比 diff 更难重建。

---

## 2026-10-25 — HTTP 桥：把统一检索接进 DSH Web UI

### 动机

`search_OMNIA` 只有模型能调。人和模型的检索入口不对称：想说「这话我之前是不是
说过」，得先让模型代劳。要给人一个入口，就需要把检索能力暴露成 HTTP。

**为什么不把检索逻辑搬到 JS 侧**：三路候选 + RRF 融合 + 归一化排序只有一份实现，
在 `omnibus.py`。让插件直连 MariaDB 再写一遍 = 制造第二份真相源（本项目吃过
「数据巴别塔」的亏）。所以 Node 只做 UI 与转发。

### 变更

**新增 `melquiades/omnibus_http.py`**（零新依赖，标准库 `http.server`）

```
GET  /health   → {ok, db, sources, modes}
POST /search   → {ok, hits, stats, notes, took_ms, limit_semantics}
POST /context  → {ok, text}
```

**与 MCP 同生共死**：桥不是独立进程，而是 MCP 进程内的 **daemon 线程**
（`_start_http_bridge()` / `_stop_http_bridge()`，照 `harvest/scheduler.py` 的模式）。

- MCP 起来桥就在，MCP 死桥就没了 → 不留孤儿进程占端口
- 端口被占（EADDRINUSE）**只告警不致命**：MCP 是本命，桥是附件
- `[bridge] enabled: false` 可整段关掉（省一条数据库连接）

**线程安全**：桥**自建一条 MariaDB 连接**，不复用 MCP 的 `_db`。
mariadb 连接对象不是线程安全的，主线程随时在跑工具、两条线程共用一个连接会互踩 cursor。

**顺带修掉一处隐患**：`do_context` 原本把桥的 db 赋值给 `server._db` 全局再调工具壳。
独立进程时无害，**进程内模式下是实打实的数据竞争**——主线程随时可能读 `_db`，
而桥线程刚把它换掉。改成把 `message_context` 的查询体抽成
`_message_context_text(db, ...)`，两边各传各的连接：

```python
# MCP 工具壳
def message_context(...):  return _message_context_text(_db, ...)
# HTTP 桥
text = mcp_server._message_context_text(st["db"], ...)
```

**踩到并修掉的坑**：`modes` 传字符串时被逐字符遍历
（`"fulltext"` → `['f','u','l',...]`），过滤后一个合法 mode 都不剩，表现为
`hits=0` 且 `took_ms=0.0`。静默出错最难查。已加 `_as_list()` 显式分流，
接受「逗号分隔字符串」与「JSON 数组」两种形状。

### 验证

- 进程内模式 6/6：起得来 / health / 全文命中 / **context 不改 `server._db` 全局** /
  停掉后端口不响应 / 端口被占时降级不抛
- 全链路（真桥 + 真 DB + 真向量路）5/5：跨语言（中文查「拜占庭」→ 命中俄文书那场对话）、
  CJK 短词「口径」、messages 命中后展开上下文
- 实测耗时：全路首次 2.0s（含 embedding 冷启动），之后 ~174ms

### 生效条件

**MCP 进程需重启**——已启动的进程不会重载模块。DSH 侧重启后桥随之上线。

---

## 2026-10-25 — CJK 检索改 bigram、新增跨源检索、多语言修正

### 动机

1. **jieba 分词会漏**：词典覆盖头部，自造词/专业术语切错边界（`布恩迪亚计算机`、
   `火用`），实测约 30% 漏搜。资料库里全是这类词。
2. **`innodb_ft_min_token_size` 默认 3 会丢掉所有 1–2 字中文词**：`口径`、`修正`
   这类词进不了索引，FULLTEXT 返回 0 而 LIKE 能中。
3. 需要一个「在草稿纸 + 会话正文 + 压缩摘要里一次找齐」的入口。

### 变更

**CJK 检索：jieba → unigram + bigram**（依据 Lucene `CJKBigramFilter(outputUnigrams=true)`）

- `melquiades/tokenizer.py`：新增 `tokenize_cjk_ub()` / `build_cjk_boolean()`。
  旧的 `tokenize()`（jieba）保留给 legacy 路径。
- `melquiades/db.py`：`content_tokenized` 由 `TEXT` 改 `MEDIUMTEXT`
  （bigram 会把文本膨胀 2–3 倍，最大 content 22,116 字符撑爆 65535）。
- `melquiades/harvest/message_runner.py`：写入时计算 `body_tokenized`。
- `melquiades/retrieval.py`：全文路改走 bigram 布尔串。
- **数据已回填**：`draftpad` 1,802 行 / `dsh_message` 14 万行 / `dsh_compaction` 181 行，
  与 LIKE 逐条对拍一致。

**非 CJK 分支扩到全 Unicode 字母**（关键修正）

```python
# 旧：只认 ASCII，西里尔/希腊/带变音拉丁整段丢弃
_CJK_CHUNK_RE = re.compile(f"[{_CJK_HAN}]+|[A-Za-z0-9_]+")
# 新：两段 alternation，顺序即优先级
_CJK_CHUNK_RE = re.compile(f"([{_CJK_HAN}]+)|(\\w+)", re.UNICODE)
```

**新增 `omnibus.py` + `search_OMNIA`**

跨 `draftpad` / `dsh_message` / `dsh_compaction` 三源，各源按规定能力跑
向量 / FULLTEXT / LIKE 三路，归一化融合。

**取数口径（用户拍板，别改回「宽取后窄截」）**：`limit` 是**每一路各取多少条**，
三路各取 limit → 按 (source, id) 去重 → **剩多少给多少，不截断回 limit**。
理由：宽取后窄截会挤掉「只有某一路命中、分数偏低」的独特结果，而那类最不该漏。

**新增 `message_context(guid, before, after)`**

`search_OMNIA` 只能给片段，看清前后文要靠它。按 `line_no` 排序取**相邻 N 条记录**
（不能按 `line_no ± N` 取窗口——line_no 是事件物理行号，抄录器跳过 chunk 家族后是稀疏的）。

**修复：会话正文命中不显示 guid**

`search_OMNIA` 的 messages 源原来只显示 `session_id`，但 `message_context` 要的是
`guid` —— 等于给了结果却接不上下一步。已改为显示完整 64 位 guid。

### 数据库调优（与 memoria 同一实例）

- `innodb_ft_min_token_size` 3 → **1**（`my.ini`，需重启；`SET GLOBAL` 会静默无效）
- `innodb_ft_sort_pll_degree` 2 → **8**、`innodb_ft_cache_size` 64M → **512M**
- `dsh_message.body_sha256` **加索引**（原本没有 → 每条 UPDATE 全表扫 14 万行，
  385 ms；一批 32 条 = 11.7 秒，占任务 73% 时间。补索引后 **188 倍**提速）

### 文档

`README.md` 加了三段警告：① Zipf 长尾 + Unicode 边缘情况 ② 数据量已到中小型网站
级别、请严肃对待性能与 MariaDB/MySQL 差异 ③ 向量检索是近似的（ANN）。

工具描述（`draftpad_find` / `archive_find` / `search_OMNIA`）补了 ANN 与多语言边界
——**下游 LLM 消费者必须知情**。


### ⚠️ 大坑：带 FULLTEXT 的表，改列注释也要重建整表

**症状**：想改 `content_tokenized` 的列注释（`jieba` → `bigram`），
`ALTER TABLE ... MODIFY COLUMN ... COMMENT '...'` 卡住/重建十几 GB。

**实测对比**：

| 表 | `ALGORITHM` | 结果 |
|---|---|---|
| **无 FULLTEXT**（试探用） | `INSTANT` | ✅ 成功，**25.9 ms** |
| **有 FULLTEXT**（真实表） | `INSTANT` | ❌ `ERROR 1846` |
| 同上 | `NOCOPY` | ❌ `ERROR 1846` |
| 同上 | `COPY` | ✅ 但**重建整表 + 重建 FTS**（17 GB → 1–1.5 小时） |

**错误信息里的 Reason 才是关键**：

```
ALGORITHM=INSTANT is not supported.
Reason: InnoDB presently supports one FULLTEXT index creation at a time.
Try ALGORITHM=COPY
```

**不是「改注释要重建」，而是「表上有 FULLTEXT 时 InnoDB 的 ALTER 实现不支持
非 COPY 算法」**——跟注释内容、跟列类型都无关。这是 MariaDB 的实现限制。

**结论 / 纪律**：

- ❌ **不要为了改一个注释单独跑 `MODIFY COLUMN`**
- ✅ **攒着**：下次真要动这张表时（改列类型、加索引、分词口径再变），
  把 `MODIFY COLUMN ... COMMENT` 一起写进**同一条 ALTER**，一次 COPY 全解决
- ⚠️ 也别想着「先删 FULLTEXT 再改」——删 FULLTEXT 本身也要 rebuild（实测 91 分钟），
  两头都亏

**当前状态**：`documents_book_chunk.content_tokenized`（memoria 库） 的注释仍是旧文案
（`应用层 jieba 分词结果`），**有意不修**，等下次重建时合并处理。
（`draftpad` 的同名列注释已是新文案——那张表小，之前顺手改过。）

### 已知边界（写清不修）

| 问题 | 现状 |
|---|---|
| 向量路是 HNSW 近似 | top-1 实测 92%、top-10 100%；**不可用于精确查找/判重** |
| 俄希拉丁在 `content_tokenized` 里为空 | **不影响查询**——非 CJK 走 `content` 列，不碰 tokenized |
| 希腊文/拉丁文屈折变化（`Πλάτων` vs `Πλάτωνος`） | 需词形还原，属独立工程，**以后再说** |
| `content_tokenized` 列注释仍写「jieba」 | 改列注释要 `MODIFY COLUMN`，不急 |
