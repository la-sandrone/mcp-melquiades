# dsh-omnibus-search

DSH Web 插件：在侧边栏里搜 **melquiades 账本**（草稿纸 / 会话正文 / 压缩摘要），
结果就地展开。

和 [dsh-fulltext-search](https://github.com/omdsh-dev/DSH-better-sidebar) 的区别：
那个搜**当前目录的文件内容**，这个搜**你和模型聊过的东西**。

## 它解决什么

「这话我之前是不是说过？」「那个结论落在哪一场？」
——DSH 的会话日志、草稿纸笔记、压缩摘要分散在三张表里，靠翻文件找不到。

这个 tab 通过本机桥把它们统一检索，三路融合：

| 路 | 擅长 | 例 |
|---|---|---|
| **vector** | 跨语言语义 | 中文查「拜占庭的体育教育」→ 命中俄文书章节 |
| **fulltext** | 单语言精确 | 查 `口径` 命中 263 条，不误伤同义词 |
| **like** | 字面兜底 | 全文分词打不中的短词/生僻串 |

## 前置：起桥

插件本身**不含检索逻辑**——它转发给 `melquiades-http`（只读、仅绑回环）。

```sh
# 在 melquiades 仓库里
melquiades-http --port 9010
# 或不经安装直接跑
PYTHONPATH=/path/to/melquiades python -m melquiades.omnibus_http --port 9010
```

验证：

```sh
curl -s http://127.0.0.1:9010/health
# {"ok":true,"db":"127.0.0.1:9002/melquiades_medicamento","sources":[...],"modes":[...]}
```

> **为什么不在插件里直连 MariaDB**：检索逻辑（三路候选 + RRF 融合 + 归一化）
> 只有一份，在 `omnibus.py`。让 JS 再实现一遍必然漂移——本仓真实吃过这个亏
> （同一字段两种序列化共存两年，15 万条数据要迁移）。所以 Node 只做 UI 与转发。

## 安装

```sh
dsh plugin --profile web add link:/path/to/melquiades/plugins/dsh-omnibus-search
```

装完需**重启 DSH** 并**硬刷新浏览器**（Ctrl/Cmd+Shift+R）。

前置：已安装 `dsh-better-sidebar`（侧边栏）。未检测到时插件只打印警告并跳过 tab
注册，**不会崩**；host 路由仍可用 curl 直接打。

## 三层开关（任一层关掉都不影响主 UI）

| 层 | 怎么关 | 关掉后 |
|---|---|---|
| ① 插件行 | `cordis.patch.yml` 里注释/删除 `- id: omnibus-search` | 完全不加载 |
| ② 配置 | `config.enabled: false` | host 不注册 `/omnia/api`，client 不注册 tab |
| ③ 依赖 | 不装 `dsh-better-sidebar` | 只有 host 路由，UI 无入口 |

## 配置

```yaml
- id: omnibus-search
  name: 'dsh-omnibus-search'
  config:
    enabled: true
    endpoint: 'http://127.0.0.1:9010'   # 桥地址（只允许回环）
    timeoutMs: 15000                    # 向量路首次冷启动实测 2.1s
    defaultLimit: 5                     # 每路各取多少条
    defaultModes: 'vector,fulltext,like'
```

配置非法（端点不是回环、modes 含未知值、limit 越界）→ **加载期直接报错**，
不静默降级。

## 用法

- 回车搜索；`↑`/`↓` 选结果；点击或 `Shift+Enter` 展开；`Esc` 收起
- 结果按来源分组：草稿纸 / 会话正文 / 压缩摘要
- 会话命中展开的是**这一条前后各 N 条**（在同一 tab 内，不跳转聊天记录）
- 徽章 `fulltext+vector` 表示这一条被哪几路命中

## 端点契约

host 侧 `/omnia/api/*` 是对桥的同名转发：

```http
POST /omnia/api/search
{"query":"拜占庭","limit":5,"modes":"vector,fulltext,like"}
→ {"ok":true,"hits":[{"source":"messages","record_id":"<guid>","score":0.4,
                     "matched_by":["fulltext"],"snippet":"…","meta":{}}],
   "stats":{...},"notes":[...],"took_ms":174.3}

POST /omnia/api/context
{"guid":"<64位hex>","before":3,"after":3,"max_chars":1200}
→ {"ok":true,"text":"会话 … · 目标 L12345 · 取回 7 条…"}
```

`limit` 是**每一路各取多少条**；三路去重后**全给、不截断回 limit**
（宽取后窄截会把「只有某一路命中、分数偏低」的独特结果挤掉，而那类恰是最不该漏的）。

## 安全

- host 路由走 fence：Host 必须回环/信任域，`sec-fetch-site` 不能是 cross-site，Origin 必须同源
- 桥只绑 `127.0.0.1` 并自己校验 Host（双层挡 DNS rebinding）
- 桥**只读**：不提供任何写入端点
- 桥不跑 `ensure_tables` / `ensure_all_vectors`——那是一次分钟级的副作用，只读桥不该碰

## 许可

Apache License 2.0。插件的 host/client 骨架参照 `dsh-fulltext-search`（同许可）。
