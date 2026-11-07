# 项目笔记

## PyTorch 全家桶版本对齐 — 史诗级巨坑

### 事故现场

```
torch      = 2.10.0+cu126    ← 正确
torchaudio = 2.11.0+cu126    ← ❌ 差一个 minor，直接炸
torchvision = 0.25.0+cu126   ← 正确（pins torch==2.10.0）
```

症状：`libtorchaudio.abi3.so: undefined symbol: torch_dtype_float4_e2m1fn_x2`

### 原因

`uv pip install torch torchaudio torchvision` 时，解析器没约束版本对齐。
torch 2.10.0 的 C++ ABI 符号表不含 `torch_dtype_float4_e2m1fn_x2`，
但 torchaudio 2.11.0 编译时链接了该符号。装是装上了，跑就炸。

### 坑在哪里

这条错配**一直存在**，但从未暴露——因为没人 import 那条路径：

```
sentence_transformers
  → transformers
    → loss/loss_rnnt.py
      → import torchaudio._extension
        → torch.ops.load_library(libtorchaudio.abi3.so)
          → 💥 undefined symbol
```

只要你的代码不经过 `transformers.loss.loss_rnnt` → `torchaudio` 这条链路，
`libtorchaudio.abi3.so` 永远不会被加载，毒永远不会发作。
装好之后随便跑 PyTorch 推理几个月都毫无问题。

直到 `sentence_transformers` 登场。它 import `transformers`，而 `transformers` 5.x 无条件 import `loss_rnnt`，后者无条件 import `torchaudio`。一次普通的 `pip install sentence-transformers`，引爆了一颗装了半年的地雷。

### PyTorch 版本对齐规则

| torch | torchaudio | torchvision |
|-------|:----------:|:-----------:|
| 2.10  | 2.10       | 0.25        |
| 2.11  | 2.11       | 0.26        |
| 2.12  | 2.12       | 0.27        |

**永远保持三者的 minor version 一致。** 没有例外。

### 教训

1. PyTorch 全家桶必须**显式锁版本**安装，不能用裸 `pip install torch torchaudio torchvision` 赌解析器
2. ABI 层面的版本错配可能**潜伏半年都不发作**，直到某一个新依赖闯进死亡 import 路径才引爆
3. `sentence-transformers` 依赖链：`sentence-transformers → transformers → (loss_rnnt) → torchaudio`，每一环都不可跳过
4. 修复：`uv pip install "torchaudio==2.10.0"`

## FastMCP.description — mcp SDK 1.12.3 break change

**症状：** `TypeError: FastMCP.__init__() got an unexpected keyword argument 'description'`

**原因：** mcp SDK ≥1.12.3 移除了 `FastMCP()` 构造器的 `description` 参数。

**修复：** 删掉 `FastMCP(description=...)` 的参数。requirements.txt 已锁死 `mcp==1.26.0`。

## pool_reset_session — MariaDB 连接器 vs MySQL 参数

**症状：** `TypeError: 'pool_reset_session' is an invalid keyword argument for connect()`

**原因：** `pool_reset_session` 是 MySQL Connector/Python 的参数，MariaDB 官方的 `mariadb` 连接器不认识它。代码是从 MySQL 示例抄的，忘了删。

**修复：** 删掉该参数行。requirements.txt 已锁死 `mariadb==1.1.14`。

---

## 终极方案

所有版本都已锁死到 `requirements.txt`：

---

# 2026-11-07 — 长任务与 DDL 的七个坑（都是实测，不是推断）

> 这一批全是「以为知道、其实不知道」的东西。每条都附判据，避免下次再靠猜。

## 一、`ADD VECTOR INDEX` 不支持 INPLACE —— 它会把整条 ALTER 拖成 COPY

一条 `ALTER` 里同时加 6 个 BTREE + FULLTEXT + VECTOR 时，走的是 COPY 算法，
逐行维护 8 个索引结构，191,913 行跑了 **50 分钟**。

很多人（包括我）会猜「FULLTEXT 和 VECTOR 都不支持」。**实测只错在 VECTOR**：

```
✓ ALTER ... ADD INDEX (sid),            ALGORITHM=INPLACE   → 接受
✓ ALTER ... ADD FULLTEXT INDEX ft (tok), ALGORITHM=INPLACE   → 接受
✗ ALTER ... ADD VECTOR INDEX vi (emb),   ALGORITHM=INPLACE   → 拒绝
    OperationalError: ALGORITHM=INPLACE is not supported for this operation. Try ALGORITHM=COPY
```

**判据**：别查文档别猜，直接 `ALTER ... , ALGORITHM=INPLACE` 打过去，引擎会告诉你。

**官方条文**（FULLTEXT 那段，坑是另一个）：
> Adding a FULLTEXT index to a table that does not have a user-defined FTS_DOC_ID column
> will require the table to be rebuilt once.
> If a table has more than one FULLTEXT index, it cannot be rebuilt by any ALTER TABLE
> when ALGORITHM is set to INPLACE.

**关键**：VECTOR 不支持 INPLACE 这件事，官方文档里**一个字都没有**，只在错误消息里。

## 二、「拆开建更快」是错的

直觉：先 INPLACE 建 6 个 BTREE，再单独建 FULLTEXT，最后 VECTOR。

算一下就错：

```
拆开：  INPLACE 建 7 个  +  COPY 重建整表（连那 7 个一起重建）
现在：  COPY 重建整表（8 个一起建）
```

COPY 那步代价一样，拆开只是多干一遍前戏。**一次性 ALTER 反而最优。**

## 三、`read_timeout: 30` 会掐死任何长查询（而且伪装成网络故障）

`mariadb.connect(read_timeout=30)` 限制的是**任何一次从 socket 读响应**的等待，
不是「空闲超时」。所以一条跑 30 分钟的 ALTER 在**第 30 秒**被**客户端自己**断开：

```
index [0/0] copy to tmp table   :  已用 00:30
mariadb.InterfaceError: Lost connection to server during query
```

**判据**：WSL 侧和 Windows 原生侧**死在同一个 30 秒**。
如果真是网络/relay 的问题，绕开 relay 的原生跑法不该同样死在 30 秒。
我当时把两次相同的时间戳当成两次不同的偶发，**没去比「两次的共同点」**，误诊成 WSL relay。

**修法**：长 SQL 用独立连接，`read_timeout=7200`。

## 四、断连 ≠ 取消

客户端 `Lost connection` 之后，**服务端那条 ALTER 继续在跑**。
实测：断开 10 分钟后它仍在 `copy to tmp table`，临时表以 ~1 MB/s 增长，
并且**一直持着元数据锁** —— 于是后续所有 ALTER 只能排队等（`Waiting for table metadata lock`），
看起来像「卡死」，实际是自己在等自己。

⇒ 要么显式 `KILL <id>`，要么等它跑完。这也正是长任务中断必须从服务端 KILL 的原因。

## 五、PCRE2 不支持 `\u` 转义（写验证 SQL 时最容易中）

```sql
WHERE body_tokenized REGEXP '[\\u4e00-\\u9fa5]{3,}'   -- ❌ 三层转义后变成 [u4e00-9fa5]
```

Python 字符串 → SQL 字面量 → PCRE 要过两层转义，`\u` 到引擎那儿已经失真；
而 MariaDB 的 PCRE2 **根本不支持 `\u`**：

```
Regex error: PCRE2 does not support \F, \L, \l, \N{name}, \U, or \u at offset 3
```

失真后 `[u4e00-9fa5]` 是一个**十六进制字符集** `{u,4,e,0,-,9,f,a,5}`，
任何 3 个连续 hex 字符都算命中 —— 实测**误报 182,126 行**，差点让我以为分词坏了。

**写法**：直接用字面汉字区间 `[一-龥]{3,}`。

## 六、长 SQL 的进度上报：官方写了会没有

MariaDB 从 5.3（2011）就有 `PROCESSLIST.STAGE / MAX_STAGE / PROGRESS`，
但文档的 **Limitations** 小节写着：

> when executing one of these commands against an InnoDB table with ALGORITHM=INPLACE
> (which is the default), progress is only reported during the merge sort phase
> while reconstructing indexes.

而 `TMP_SPACE_USED`（11.5 起）**明确不含** `alter table` 的引擎内部临时文件：
> It does not include engine internal temporary files used for repair, alter table,
> index pre sorting etc.

所以本次全程 `STAGE=0 / MAX_STAGE=0 / PROGRESS=0.000 / TMP_SPACE_USED=0` —— **不是 bug**。

**可移植的兜底信号**：`#sql-alter-*.ibd` 的文件大小 ÷ 源表大小。
过滤时要**排除 `#i#` 开头的 FTS 附属文件**，同一条 ALTER 会生成多个。

## 七、长任务要开子进程，不能只开子线程

| | 能否无条件终止 | 依赖 |
|---|---|---|
| 线程 | **不能** | 必须合作（查标志位）或存在外部取消句柄 |
| 进程 | **能** | 内核直接回收 |

Python 的坑更深一层：**信号只投递给主线程**，子线程卡在 C 调用里同样收不到 `KeyboardInterrupt`。
Win32 对此有专名 —— `CancelSynchronousIo(hThread)`：从外部取消阻塞调用。
对应到数据库就是「另一个连接发 `KILL <id>`」。

**判据**：有外部取消句柄（DB 查询 / HTTP）→ 子线程 + 外部取消；
没有（CPU 密集、第三方 C 库）→ 子进程 + SIGKILL。

另外：**`.cmd` 文件必须 ASCII-only**。cmd.exe 按 GBK 解析，UTF-8 中文注释会破坏行解析
（实测：注释里的中文被当成命令执行）。中文文档放 `.md` 和 `.py` 里。

## 八、零向量污染 HNSW —— 这条最反直觉

`VECTOR(1024) NOT NULL DEFAULT 零向量` 意味着**每一行都占 4 KB**，不管有没有真向量。
本次 191,913 行里只有 40,698 行（21%）有真向量，其余 15 万条是**完全相同的零向量**。

它们全都进了 HNSW 图。结果：**图和召回一起坏**。

| | @5 | @10 | MRR |
|---|---|---|---|
| 索引含 15 万零向量 | 5/12 | 7/12 | 0.243 |
| **索引只含 4 万真向量** | **9/12** | **10/12** | **0.336** |

⇒ **「只索引有真数据的行」不是省空间，是质量修复。**
分表正好天然实现这一点（侧表只 INSERT `embedded=1` 的行）。

## 九、`DROP COLUMN` 可能比想象快两个数量级

预计 30–40 分钟的重建，实际 **12 秒**。
`DROP COLUMN` 在 MariaDB 上是 INPLACE —— 只改表定义、不重写数据页。

反过来是个有用的结论：**那 50 分钟全花在 VECTOR 的 COPY 上**，与表的其余部分无关。
