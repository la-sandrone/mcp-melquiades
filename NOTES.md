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
