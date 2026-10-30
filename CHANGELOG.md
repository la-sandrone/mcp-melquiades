# CHANGELOG

格式：按日期倒序。每条记「**为什么**」而不只是「改了什么」——因为半年后回来看，
动机比 diff 更难重建。

---

## 2026-10-31 — 110 排查的否证记录：offload 不可行，只剩等微软

**这一条不记"改了什么"，只记"试过什么、为什么没用"。**
留它的唯一目的：让下一个人（很可能还是我）不必再花一个晚上重复这三个实验。

### 否证一：随机 SYN 丢包（09-22 的原始结论）

把 5% 失败率反推成单包丢包率：p⁴ = 0.05 ⇒ **p = 0.473**。loopback 上不可能。
模型自相矛盾，作废。真正机制见 2026-10-30 条目（relay 改错端口）。

### 否证二：端口耗尽 / TIME_WAIT

- 症状不匹配：端口耗尽是 `EADDRNOTAVAIL(99)`「Cannot assign requested address」，
  不是 `ETIMEDOUT(110)`。
- 水位不匹配：对 9002 的 TIME_WAIT 峰值 660 / 临时端口池 4096 = **16%**。
- 已有缓解：`tcp_tw_reuse=2` 在 loopback 上允许复用。
- 顺带否掉的传闻：Windows 动态端口范围异常 —— 实测 `netsh int ipv4 show dynamicport tcp`
  = `49152 + 16384`，默认值。

### 否证三：checksum offload（2026-10-30 20:42 → 10-31 06:23，约 9.7 小时）

```
sudo ethtool -K loopback0 tx off        # 假设：relay 对"推迟计算"的校验和补算错位置
```

| 窗口 | 事件 / harvest 圈 | 比例 |
|---|---|---|
| 改前基线 | 47 / 268 | 17.5% |
| 改后 9.7h | **55 / 172** | **32%** |

**无改善**；改后第一次失败在 `20:43:37`，距改动仅 37 秒。已回滚
（`ethtool -K loopback0 tx on`），连通性复查 OK，系统无残留。

**为什么这个假设从一开始就不该被指望**（方向性错误，记下来以免重犯）：

`#40343` 的抓包里 **SYN 发出去时端口是对的**（41264），**回来时才错**（指到 36677）。
⇒ 病灶在**返回路径**，与 WSL 的发送侧无关，关 tx offload 命中不了。
而 `#41137` 的「端口值 = 从 TCP 头前 14 字节起算的校验和」说明是**生成侧**把校验和
写到了错偏移 ⇒ **接收侧（rx offload）怎么设都算不回来，故 rx off 也不必试。**

### 顺带测清的结构事实：失败成「坏窗」，不是均匀分布

整夜 **136 次事件 / 65 个坏窗**：多数坏窗只 1 次（≈6s 后自己恢复）；
最长一窗 `04:20:00` 起**连续 10 次 ≈ 60 秒**，那一分钟里每条连接都失败。

三条推论（其中两条推翻了 10-30 条目里的说法）：

1. **A 的收益集中在"孤立命中"**：命中一次省 25s（30s→6s）。
   但**长窗下超时长短等价**——都卡到窗口结束为止。
   故 10-30 条目里"一夜省下 22 分钟"的说法**粗糙，作废**。
2. **保活（D）划掉**：坏窗内每条连接都失败，保活只会多刷日志。
   它只对"空闲导致映射失效"的模型有效，而那个模型不成立。
3. **`DB 连接连续失败` 整夜 0 条**：7 次尝试从未抽空，`connect_retry_budget_sec=60` 够用。

### 连带作废的其他路线

- **B（mysqld bind LAN 地址绕开 relay）**：用户口径是"一旦暴露就按公网服务器精神防御"，
  而当前 `ssl_cert=NULL`（TLS 根本没证书）、`local_infile=1`、单账号全库读写——
  为少几条 110 去配齐这一整套，性价比不成立。且 mirrored 下发夹流量的源地址
  就是宿主机自己，防火墙无法区分"来自 WSL"与"来自局域网邻居"，最小暴露表达不出来。
- **切回 NAT**：mirrored 是刚需（大量服务在 WSL↔Windows 边界上来回），出局。
- **C（MariaDB 搬进 WSL）**：9p 文件性能 / VHDX 膨胀，且已有 20~30GB 生产数据，否决。
- **"localhost 域名 + 动态解析"**：能让 NAT 重新可行从而根除，但它引入一个必须永活的
  服务发现层——为 6 秒的偶发卡顿新增一个单点故障，账不成立。

### 结论

能做的都做完了，**剩下的只有等微软修**。拆除条件见 `config.yaml` 同段注释
（#40343 或 #41137 关闭 + 「睡 60s → 连一次」×300 轮 0 命中）。
6 秒的偶发卡顿全部发生在后台 harvest 线程，不影响交互。

---

## 2026-10-30 — 删除旧连接实现（db.py / search.py），并把连接重试改成「短超时 + 多尝试」

### 动机

两条线，其实是同一件事的两面：**把"遗留"和"照旧"都清掉**。

1. **旧实现真的没人用了**。`db.py` 是铁律一之前的连接池版本（614 行，`class Database`），
   `search.py` 的存在意义就是消费它（`Searcher.__init__(db: "Database")` 还留着一个
   指向已死类的字符串注解）。全仓核查：无 `import`、无 `importlib`/`__import__`
   动态加载、无字符串形式的模块名引用。DESIGN.md 早就拍板"去池化"、IMPLEMENTATION.md
   也记着"属遗留"，但文件一直躺在包里——**记录写了不等于执行了**。
2. **旧超时标定是错的**。09-22 的结论把 `Can't connect to server on '127.0.0.1' (110)`
   判成"偶发 SYN 丢包"，于是把 `connect_timeout` 从 10 提到 30 来"容忍"。
   实际病因是 WSL2 mirrored 的策略路由——127/8 的 TCP/UDP 被拽出 `lo` 走 Windows
   侧 loopback relay，relay 偶发改错端口（microsoft/WSL#40343、#41137、#10855，均 open）。
   于是 SYN 的重传**被同样地改错**，必然吃满整个闸门；下一次连接换端口才通。

### 变更

**1. 删除死代码**
- `melquiades/db.py`（614 行，连接池）、`melquiades/search.py`（只消费前者）
- `README.md` 目录树、`IMPLEMENTATION.md` 待办 #7、`DESIGN.md` 决策记录同步标注

**2. 重试参数重标定（配置 + 默认值）**

| 参数 | 旧 | 新 |
|---|---|---|
| `connect_timeout` | 30 | 5 |
| `connect_retries` | 3 | 6（共 7 次尝试） |
| `connect_retry_budget_sec` | 45 | 60 |

依据：`web.log` 里 792 次失败**全部**是"第 1/4 次、已耗时 30.0s"（重试一次必成），
而黑洞窗口按**挂钟时间**关闭（15:37:48 失败 / 15:37:49 成功）——等得久买不到成功率，
只买到痛苦。短超时把单次卡顿从 30s 压到 5s 级，靠次数覆盖同一窗口。

**3. 修掉"预算不是硬上限"**：旧实现只在退避**前**查预算，30s×4 实测跑到 61s，
而配置声明是 45s。现在每次尝试前按剩余预算裁剪单次 `connect_timeout`。

**4. 噪声闸门**：首次失败 WARNING、后续降 INFO、最终连上补一条
`DB 连接已恢复（此前失败 N 次，累计 Xs）`——短超时下一个窗口会连出好几条相同告警，
不压的话比原来更吵。

**5. 顺手改掉一处失真的注释**：`tools/replay/conn_forensics.py` 的 `SLOW_SEC`
原写着"与 MariaDB 配置 connect_timeout=10 同源"，早已不是那个值，也不再需要同源。

### 验收

`tools/replay/verify_db_retry.py` 六条全 PASS，新增第 ⑥ 条为预算硬上限的回归锁
（budget=8s + timeout=5s × 7 次，实测 7.01s 停在预算内）。

### 拆除条件（别半年后又来重查一遍为什么是 5 而不是 30）

**两条都满足**才动这组参数：

1. `microsoft/WSL#40343` 或 `#41137` **关闭**，且 release note 提到 loopback 的
   port / checksum 修复。升级 WSL 后**先看这两条的 state，别只看版本号**——
   本机 WSL 3.0.3.0 时它们全 open，且 2026-10-24 还有人开同类新 issue。
2. 跑一次「睡 60s → 连一次」×300 轮，**0 次命中**。
   ⚠️ 连打不睡是另一个 regime：100 次连打对 `127.0.0.1:9999` 与
   `192.168.10.229:9999` 都是 100% 立刻 RST，**测不出任何东西**。

两条都成立 → 可把 `connect_timeout` 调回 15~30 并摘掉 `dbcore` 的噪声闸门。
只成立 ① → 留参数（可能还有别的黑洞）；只成立 ② → 留参数（可能只是运气）。

### 上线后首批实测（2026-10-30 20:19，重启加载新代码）

```
20:19:43 WARNING  DB 连接失败（第 1/7 次，单次上限 5s，累计 5.0s），1.0s 后重试：(110)
20:19:44 WARNING  DB 连接已恢复（此前失败 1 次，累计 6.0s）
20:19:49 WARNING  DB 连接失败（第 1/7 次，单次上限 5s，累计 5.0s），1.0s 后重试：(110)
20:19:50 WARNING  DB 连接已恢复（此前失败 1 次，累计 6.0s）
```

单次事件 **30s → 6.0s**，`DB 连接连续失败` 0 条。

> **2026-10-31 修正**：本节曾据此断言"中招按连接发生、**不是**持续窗口"。
> 整夜数据推翻了这个二选一——**两种形态都有**：既有孤立命中，也有持续 60 秒的坏窗
> （`04:20:00` 起连续 10 次失败）内每条都失败。结论（短超时 + 多尝试最优）不变，
> 但"坏窗内超时长短等价"这一条要记住。详见 2026-10-31 条目。

这组数据当时还**修正了一条错误推理**：原先以为"黑洞窗口按挂钟时间关闭"，
据此改判为"中招按连接发生、**不是**持续窗口"。
（后来证明这个二选一本身就是错的——两种形态都有，见上方 10-31 修正。
结论始终不变：短超时 + 多尝试；变的只是理由。）

### 不动的东西

- 查询**途中**失败仍绝不自动重试（写重放会双插）——铁律没动。
- `schema.py` 保留（它 `ensure_all_vectors` / `ensure_embedded_column` /
  `ensure_vocab_tables` 都在用，接收的正是 `dbcore.MariaDB`）。

---

## 2026-10-25 — 桥的端口发现与失败可诊断（补 0.2.1 的 webRuntime 陷阱）

### 动机

桥接进 DSH 后暴露两个问题，都是「配置要人肉对齐」造成的：

1. **端口写两处**：桥监听哪个端口在 `config.yaml` 的 `[bridge] port`，插件去哪个
   端口找在 `cordis.patch.yml` 的 `endpoint`。不一致的症状只是「连不上」。
2. **「连不上」吞掉了所有死法**：MCP 没起 / 端口被占 / 桥线程退 / 配置关掉，
   在插件侧完全同形。

顺带记一个**装不上的坑**（不属于桥本身，但同批踩的）：
DSH `0.2.1-alpha.2` 起 `webRuntime` 服务被移除、改 `webStartup`。官方原文：
「A profile that injects `webRuntime` now **waits for a service that never mounts**」。
插件的 `apply()` 因此从未执行、路由从未注册，表现为侧栏 `HTTP 405`——而 405 来自
静态 dist 兜底，不是插件代码。**症状指向请求处理，真凶在注册环节。**

### 变更

**发现文件 `~/.melquiades/bridge.json`**

桥启动时写、退出时删。插件每次请求前读它（端口**以桥为准**，`endpoint` 只是兜底）：

| 发现文件 | 插件行为 |
|---|---|
| `ok:true` | 用它写的 `base` |
| `ok:false` | 桥尝试过但失败 → 把 `error` 原文给用户 |
| 不存在 | 回退 `endpoint`，报错指向「MCP 可能没起」 |

失败路径也写文件（`_write_bridge_failure`），所以「为什么没起」不需要翻日志。

**`/health` 补运行态**：`uptime_s` / `pid` / `requests` / `started_at`。

**保活：明确不做。** `serve_forever()` 不会自己退出；唯一会死的是端口被占，
而那重试只会与手动跑的 standalone 抢端口。改为把死法如实暴露。

**插件侧 `inject` 修正**：`webRuntime` → `webStartup`；`dsh.client.inject` 与
`peerDependencies` 删掉从未 import 的包（声明不存在的模块只会多一个失败面）。

### 一个自己造又被自己抓到的静默错误

`readDiscovery()` 最初写成 `require('node:fs')` —— host 是 **ESM**，`require` 不存在，
异常被本函数的 `catch` 吞掉 → 永远返回 `null` → **发现机制看似实现、实际从不生效**。
所有测试「通过」，只是那条分支永远走不到。教训与「以静默降级为耻」同源：
`catch { return null }` 这种兜底，必须在注释里写清「什么情况下允许它发生」。

### 验证

发现机制 8/8（桥写文件 / health 新字段 / 插件忽略过时 endpoint / 三种死法可分辨 /
退出时清理）；插件静态 17/17、转发 e2e 9/9。

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
