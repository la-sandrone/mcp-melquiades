"""
文件名: melquiades/config.py
摘要: 配置加载 —— **所有魔法数字的唯一出口**（铁律三）。
      YAML 优先，缺失项回落本文件内的默认值；DSH_HOME 按唯一口径逐级解析。
依赖: yaml（可选，缺失时纯用默认值）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


# ──────────────────────────────────────────────
# DSH_HOME 解析（唯一口径）
# ──────────────────────────────────────────────

def _derive_home_from_version(harness_dir: str) -> Optional[str]:
    """读 <harness_dir>/DSH_VERSION，派生出 <harness_dir>/dsh-domus-<版本>。

    home 名自 2026-09-26 起是【规则】不是常量：dsh-domus-<版本去前导 v>
    （例 v0.1.7-rc.2 → dsh-domus-0.1.7-rc.2）。既然真相源是 DSH 自己的
    版本选择器文件，这里就直接引它，而不是抄一份结果。

    返回 None 表示"派生不出 / 目标目录不存在"，交由调用方继续走回落链
    —— **绝不返回一个不存在的路径**。那正是 2026-09-26 事故的形态：
    路径已失效却无人报错，收割只是 files=0/0，看起来像"没有新数据"。
    """
    try:
        version_file = os.path.join(harness_dir, "DSH_VERSION")
        if not os.path.isfile(version_file):
            return None
        version = ""
        with open(version_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("DSH_VERSION="):
                    version = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        if not version:
            return None
        candidate = os.path.join(harness_dir, "dsh-domus-" + version.lstrip("v"))
        return candidate if os.path.isdir(candidate) else None
    except Exception:
        return None


def resolve_dsh_home(override: Optional[str] = None, harness_dir: Optional[str] = None) -> str:
    """
    解析 DSH_HOME。逐级回落，**永不抛、永不返回空串**。

      ①  override（配置显式指定，最高优先；留空则跳过）
      ①b harness_dir + DSH_VERSION 派生 —— 优先于环境变量
      ②  环境变量 DSH_HOME
      ③  os.path.expanduser("~") + /.dsh
      ④  USERPROFILE || HOME + /.dsh
      ⑤  ".dsh"（相对路径兜底）

    为什么有 ①b（2026-09-26 事故）：
      项目与 home 都改用精确版本名（dsh-domus-0.1.7-rc.2）之后，旧配置里
      写死的 `home: .../dsh-domus` 当天就失效 —— 收割静默归零，日志只有
      一句 "sessions 目录不存在"，files=0/0。看起来像"没新数据"，实则断流。
      home 名既然是【由版本派生的规则】，就该引真相源（DSH 的 DSH_VERSION）
      而不是抄一份结果。与 harvest/runner.discover_logs 的
      "不追名单，追规则"同一哲学：改名与升级当天都不再断流。

    纪律（抄自 dsh-auto-memory/lib/dsh-home.js 的事故记录）：
    只读环境变量、不缓存、永不抛、永不返回空串。
    """
    # ① 显式覆盖
    try:
        if override is not None and str(override).strip():
            return str(override).strip()
    except Exception:
        pass

    # ①b 由版本派生（harness_dir/DSH_VERSION → dsh-domus-<版本>）
    try:
        if harness_dir is not None and str(harness_dir).strip():
            derived = _derive_home_from_version(str(harness_dir).strip())
            if derived:
                return derived
    except Exception:
        pass

    # ② 环境变量
    try:
        env = os.environ.get("DSH_HOME", "")
        if env and env.strip():
            return env.strip()
    except Exception:
        pass

    # ③ ~/.dsh
    base = ""
    try:
        base = os.path.expanduser("~")
    except Exception:
        base = ""

    # ④ USERPROFILE || HOME
    if not base:
        try:
            base = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
        except Exception:
            base = ""

    if base:
        return os.path.join(base, ".dsh")

    # ⑤ 兜底（保证非空）
    return ".dsh"


# ──────────────────────────────────────────────
# 配置分段
# ──────────────────────────────────────────────

@dataclass
class MariadbConfig:
    """MariaDB 连接（即弃连接，铁律一——无连接池字段）。"""

    host: str = "127.0.0.1"
    port: int = 9002
    database: str = "melquiades_medicamento"
    user: str = "melquiades"
    password: str = ""            # 无默认值语义：必须由 config.yaml 提供
    charset: str = "utf8mb4"
    # ── 连接超时与**建立连接阶段**的重试（2026-10-30 重标定，推翻 09-22 的 10→30）──
    # `connect_timeout` 覆盖 TCP connect + 握手 + 认证整个阶段。
    # ★ 病因（2026-10-30 定案）：WSL2 mirrored 下 127/8 的 TCP/UDP 被策略路由拽出 lo
    #   （ip route get 127.0.0.1 → ... dev loopback0 table 127），连本机 Windows 服务
    #   实际走 loopback relay；relay 偶发改错端口/校验和 ⇒ 内核认不出 ⇒ SYN 重传全废
    #   ⇒ 吃满 connect_timeout ⇒ errno 110。重试换端口常常就好。
    #   上游 open：microsoft/WSL#40343 / #41137 / #10855。
    # ★ 为什么 30 → 5：**T 只决定单次失败的"价格"，不是成功率**。
    #   实测：T=30 时生产日志 792 次失败全是"第 1/4 次、耗时 30.0s"（每次吃满闸门）；
    #   T=5 上线后首批事件是"第 1/7 次、5s" → 1s 后恢复，**累计 6.0s**。
    #   中招按**连接**发生（20:19:44 刚成功、20:19:49 又失败 ⇒ 不是持续窗口），
    #   所以短超时省"价格"、多尝试压"连续中招"的尾部。两头都赚，无取舍。
    #   ★ 拆除条件与完整取证见 config.yaml 同段注释（两条都满足才调回 15~30）。
    connect_timeout: int = 5
    # 建立连接失败的重试次数。**只重试"建立连接"阶段**——
    # 此时尚未发出任何语句，重试幂等；查询途中失败**绝不自动重试**
    # （写操作重放会双插，且无法判断服务端是否已执行）。
    connect_retries: int = 6
    connect_retry_backoff_sec: float = 1.0
    # 整个重试循环的**总耗时预算**，真硬上限：dbcore 按剩余预算裁剪单次 connect_timeout
    # （2026-10-30 修正：旧实现只在退避前查预算，30s×4 实测跑到 61s）。
    # 6 次重试 + 5s 超时最坏 ≈ 56s ⇒ 设 60，保住旧配置（30s×2 ≈ 61s）的覆盖时长。
    connect_retry_budget_sec: float = 60.0
    read_timeout: int = 30
    write_timeout: int = 30
    # DDL/迁移开关（测试环境可关）
    ensure_tables: bool = True


@dataclass
class DraftpadConfig:
    """草稿纸模块。"""

    default_bucket: str = "default"          # 已拍板：session_id 降级为 bucket，默认 default
    content_max_chars: int = 60000           # 写入截断上限
    scan_default_width: int = 400            # 游标读的 LEFT() 截断宽度
    scan_default_limit: int = 50             # 游标读默认条数
    scan_max_limit: int = 500
    find_default_limit: int = 5
    find_max_limit: int = 50
    note_key_max_chars: int = 128


@dataclass
class DshConfig:
    """DSH 会话日志（收割源）。"""

    # ── home 解析（2026-09-26 起不再写死版本号）──
    # 留空 home + 给出 harness_dir ⇒ 由 <harness_dir>/DSH_VERSION 派生
    #   dsh-domus-<版本去前导 v>    （例 v0.1.7-rc.2 → dsh-domus-0.1.7-rc.2）
    # 理由见 resolve_dsh_home 的 ①b：写死 home 的代价是改名/升级当天静默断流
    # （2026-09-26 实测 files=0/0，只报一句 "sessions 目录不存在"）。
    harness_dir: Optional[str] = None        # 例 /path/to/deepseek-harness
    home: Optional[str] = None               # 显式指定则优先（逃生口）；留空 = 派生
    sessions_dir: str = "sessions"
    # ⚠️ 已淘汰（2026-09-26）：日志发现改为【按文件名解析代数、取最大】
    #    （见 harvest/runner.discover_logs），因为固定名单会把 v4 及以后
    #    整个漏掉、并让"多代并存"目录盯住冻结的旧代际。此字段保留仅为
    #    兼容既有配置，不再参与发现逻辑。
    log_names: tuple[str, ...] = ("session.v3.jsonl.zstd", "session.jsonl.zstd")
    harvest_enabled: bool = True
    scan_interval_sec: int = 60              # 已拍板：定时器 60s
    startup_scan_enabled: bool = True         # 已拍板：MCP 加载时自动扫一轮
    startup_scan_dirs_limit: int = 10         # 启动扫最多处理多少个会话目录
    # 工具搭车的最小间隔（秒）—— 防止每次工具调用都触发一轮文件系统扫描
    piggyback_min_interval_sec: int = 30
    # 孤儿运行记录判定（分钟）：超过此时长仍为 running 的记录，启动时标记为 interrupted
    stale_run_minutes: int = 10
    # 首扫（游标表为空）不走启动路径，由 CLI --backfill 手动跑
    backfill_max_files: int = 0               # 0 = 不限
    backfill_max_frames_per_file: int = 0     # 0 = 不限
    max_frames_per_cycle: int = 400           # 单轮最多解压帧数（限速）
    stop_wait_timeout_sec: float = 5.0        # 收割线程停止等待上限


@dataclass
class HarvestConfig:
    """压缩档案入库。"""

    summary_max_chars: int = 200000           # 摘要/防漏网层截断上限
    shadowed_seqs_max: int = 20000            # 遮蔽 seq 列表上限（防爆）
    keywords_max: int = 400                   # L3 关键词集上限
    excerpt_chars: int = 200                  # L3 每条被遮蔽消息首段长度
    log_default_limit: int = 50


@dataclass
class SearchConfig:
    """检索栈（多路 + 兜底链）。"""

    # 嵌入端点**按序尝试**（实测 2026-09-22：:9005 本地优先端点超时、:9006 可用）
    bge_endpoints: tuple = (
        "http://localhost:9005",
        "http://localhost:9006",
    )
    # ★ 必须带 `remote:` 前缀。端口语义（读 server_bge.py 得到）：
    #   9005 = 本地优先、9006 = 远端优先；`remote:` 是**硬覆盖**。
    #   不写前缀 ⇒ 由端口决定 ⇒ 9006 挂掉回退到 9005 时**静默换成本地权重**
    #   （向量不同，max|Δ|=1.6e-3；且本地嵌入 24s vs 远端 0.1s，慢 150 倍）。
    #   实测 9006 上「无前缀」与「remote:」向量逐位相同 ⇒ 加前缀不作废库内已有向量。
    bge_model: str = "remote:BAAI/bge-m3"
    embedding_dim: int = 1024
    fts_weight: float = 0.5
    vector_weight: float = 0.5
    keyword_weight: float = 0.3
    # ── 融合常数（2026-09-22 实测标定，见 tools/replay/REPORT.md §八）──
    # RRF 的平滑常数：`score += weight / (rrf_k + rank + 1)`。
    # 注意本项目**原实现等价于 rrf_k = 0**（最朴素的 1/rank），业界默认 60。
    # 本语料实测**非单调**、甜点为 10（MRR 0.4041 → 0.4398，+8.8%；30/60 回落）
    # ⇒ 不要抄默认值，改它之前请复跑 sweep_fusion.py。
    rrf_k: int = 10
    # ── 线索路（逐线索检索 + IDF 过滤 + 集成）──
    # 单条线索的召回深度；**同时充当「高频线索」的判定阈值**：
    # 返回条数达到该上限 ⇒ 视为 IDF 极低（如 `AU`/`de`）⇒ 不进集成。
    clue_depth: int = 10
    clue_max: int = 12              # 单次最多处理多少条线索（防病态长查询）
    clue_min_chars: int = 2         # 清洗后短于此长度的线索直接丢弃
    clue_weight_ratio: float = 0.5  # 线索路权重 = fts_weight * 本值（实测半分最佳）
    # ── 查询/结果不确定性的提示阈值 ──
    # `sep5_low` = 「前5分离度」低于此值 ⇒ 提示"结果区分度低"。
    # 取值依据：232 条真实查询实测 p25 = 0.316（p50 = 0.419、p10 = 0.193）。
    # ★ 为什么不用 p50：分离度是**连续分布、不是双峰**，取 0.419 会覆盖 50% 的查询
    #   ⇒ 提示泛滥就变成狼来了。取 p25 覆盖约 1/4。
    sep5_low: float = 0.316
    # 「模糊/语义模式」（mode=vector，专给"想不起来了"用）的**默认返回条数**。
    # 比精确模式的 5 更大：记不清时本来就该多看几条才谈得上挑。
    # ★ 注意：这里**不是**"加宽 recall_k"——单路模式下排序是全局的，
    #   取前 50 再截前 5 与直接取前 5 完全等价（数学上的 no-op）。
    #   真正有意义的是"返回多少条"与"喂给重排的池多大"。
    semantic_default_limit: int = 10
    uncertainty_hint: bool = True   # 总开关（只影响提示文案，不改变任何检索行为）
    bge_connect_timeout_sec: float = 5.0
    bge_timeout_sec: float = 60.0     # BGE 懒加载可能耗时数十秒
    # 受控词表（SKOS 同义词环）的查询扩展开关。
    # 依据 ISO 25964-2 §24.3.2：同义词环的主用途就是**查询扩展**——
    # "mappings from thesaurus concepts to synonym rings should be used to
    #  convert search queries"（提召回）。★ 只并进**字面两路**，不动向量查询。
    vocab_expand_query: bool = True
    # 层级（skos:broader/narrower）扩展：查 `黑洞` 连带上**下位**词 `克尔黑洞`。
    # 依据 ISO 25964-2：搜索词表支持沿 broader/narrower 扩展。
    # ★ 默认**只向下** —— 向上扩展会把结果泛化（查 `克尔黑洞` 混进全部黑洞资料），
    #   精度损失大于召回收益。方向由 vocab.expand_hierarchy 的参数控制。
    vocab_expand_hierarchy: bool = True
    # 兜底链开关：融合空结果 → LIKE 串联
    fallback_to_like: bool = True
    # 向量距离上限（超过则丢弃；0 = 不过滤）
    vector_max_distance: float = 0.0
    # ── 跨编码器精排（2026-09-22 上线）──
    # 在融合序候选池上重排，**默认开**；工具参数 `rerank=False` 可单次关掉。
    # 行为标签实测（193 条 used 标签，池=30，看 top-5）：
    #   auto   hit@5 114→127（+13）、MRR 0.4331→0.5271（+0.094）
    #   vector hit@5  98→116（+18）、MRR 0.4281→0.5327（+0.105）
    # ★ 它**补不了召回**：未进池 28/45 开关重排后完全不动（池外的东西它看不见）。
    # ★ 不作用于 mode=keyword（LIKE 串联）与 mode=condition（原样 SQL 探针），
    #   也不作用于 auto 的 **LIKE 兜底**分支 —— 兜底语义是"退到字面查找"，
    #   再叠一层语义精排会让"兜底"名不副实。
    rerank_enabled: bool = True
    # `remote:` 前缀 ⇒ 走代理的上游 API（有 RPM/TPM 令牌桶，不易被拒）。
    # 去掉前缀会打到代理本地权重，行为不同 —— 别顺手改。
    rerank_model: str = "remote:BAAI/bge-reranker-v2-m3"
    # 重排服务端点。**留空 ⇒ 回退到 bge_endpoints**（当前同机 9005/9006）。
    # 独立可配的意义：单侧故障（重排挂、嵌入好）是必须能单独验证的形态，
    # 绑在一个字段上就造不出来这种场景。
    rerank_endpoints: tuple = ()
    # ── 按表启用/按表字数 ──
    # 表 → 是否重排。**档案表默认关**（2026-09-22 实测：见 rerank_doc_chars 注释）。
    # 未知表名会让 Config.load 直接报错（**不静默忽略** —— 拼错表名却被当成"关掉了"
    # 是最难查的一类配置 bug）。
    rerank_tables: dict = field(
        default_factory=lambda: {"draftpad": True, "dsh_compaction": False})
    # 喂给重排的候选池大小（**不是越大越好**，实测标定见 config.yaml 注释）
    rerank_pool: int = 30
    # 喂给重排的**正文字数上限**。必须取自正文（非展示口径 LEFT(col,400)）：
    # draftpad 正文均长 1390、dsh_compaction 摘要均长 10665，
    # 拿 400 字喂等于用 29% / 3.8% 的文档做精排。800 是实测口径。
    # 按表的正文字数上限（缺省回退到 Hit.content 的展示宽度 = 不用额外查库）。
    # ★ draftpad = 400（实测最优）。193 条行为标签、生产路径、池=30：
    #     doc_chars=400  → hit@5 128 / MRR 0.5303 / p50  655ms   ← 最优
    #     doc_chars=800  → hit@5 126~127 / MRR ~0.5091 / p50 2260ms
    #     doc_chars=1200 → hit@5 123 / MRR 0.4942 / p50 3472ms
    #   随长度**单调变差**（MultiConIR：重排器随文档长度衰减/注意力弥散），
    #   成本亦随长度上升 ⇒ 无取舍。★ 别凭"取正文更全"的道理往上调，先复跑
    #   tools/replay/verify_rerank_metrics.py --doc-chars-sweep。
    rerank_doc_chars: dict = field(default_factory=lambda: {"draftpad": 400})
    rerank_connect_timeout_sec: float = 10.0
    # 读数超时。实测 p50 ≈ 1.1s（池=30）；量测脚本当时给 180s 是因为批量跑不赶时间，
    # 交互式工具调用不能挂 3 分钟 ⇒ 收紧到 30s（只影响失败判定，不影响排序结果）。
    rerank_timeout_sec: float = 30.0
    rerank_retries: int = 3
    rerank_backoff_sec: float = 2.0


@dataclass
class BridgeConfig:
    """HTTP 桥：把统一检索暴露成 JSON 端点，供 DSH Web UI 转发。

    **与 MCP 同生共死**：桥不是独立进程，而是 MCP 进程内的 daemon 线程。
    MCP 退出 ⇒ 线程随之回收；桥崩了也只影响 UI，不影响 MCP 本体。

    `port` 被占用时**只告警不致命**——MCP 是本命，桥是附件。
    那种情况下 DSH 的搜索 tab 会不可用，但其余工具照常。
    """

    enabled: bool = True
    host: str = "127.0.0.1"     # 只绑回环：桥是只读的，但别把它变成一个对外打洞的代理
    port: int = 9010


@dataclass
class Config:
    """总配置。"""

    mariadb: MariadbConfig = field(default_factory=MariadbConfig)
    draftpad: DraftpadConfig = field(default_factory=DraftpadConfig)
    dsh: DshConfig = field(default_factory=DshConfig)
    harvest: HarvestConfig = field(default_factory=HarvestConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    bridge: BridgeConfig = field(default_factory=BridgeConfig)

    # ── 派生 ──

    @property
    def dsh_home(self) -> str:
        """解析后的 DSH_HOME（唯一口径）。"""
        return resolve_dsh_home(self.dsh.home, self.dsh.harness_dir)

    @property
    def sessions_path(self) -> Path:
        """会话日志根目录。"""
        return Path(self.dsh_home) / self.dsh.sessions_dir

    # ── 加载 ──

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        """
        从 YAML 加载配置；文件不存在或缺 yaml 时返回全默认值。

        解析策略：只覆盖 YAML 中**显式出现**的键，其余保持默认。
        """
        raw: dict[str, Any] = {}
        if path:
            p = Path(path)
            if p.is_file():
                if yaml is None:
                    raise RuntimeError(
                        f"配置文件存在但 pyyaml 未安装: {p}"
                    )
                with p.open("r", encoding="utf-8") as fh:
                    loaded = yaml.safe_load(fh) or {}
                if not isinstance(loaded, dict):
                    raise ValueError(f"配置根节点必须是映射: {p}")
                raw = loaded

        cfg = cls()
        cfg._apply(raw.get("mariadb"), cfg.mariadb)
        cfg._apply(raw.get("draftpad"), cfg.draftpad)
        cfg._apply(raw.get("dsh"), cfg.dsh)
        cfg._apply(raw.get("harvest"), cfg.harvest)
        cfg._apply(raw.get("search"), cfg.search)
        _validate_rerank_tables(cfg.search)
        return cfg

    @staticmethod
    def _apply(section: Any, target: Any) -> None:
        """把 YAML 段里**已声明**的字段覆盖到 dataclass 实例上。"""
        if not isinstance(section, dict):
            return
        valid = {f for f in target.__dataclass_fields__}  # type: ignore[attr-defined]
        for key, value in section.items():
            if key in valid and value is not None:
                setattr(target, key, value)


# ──────────────────────────────────────────────
# 默认配置文件路径
# ──────────────────────────────────────────────

DEFAULT_CONFIG_FILENAME = "config.yaml"


def _validate_rerank_tables(search: Any) -> None:
    """规范化并校验 rerank_tables / rerank_doc_chars（**坏配置要响亮地炸**）。

    为什么单独校验：配置是从 YAML 直接 setattr 的，写错类型不会报错，
    要等到检索时才以奇怪的 AttributeError 暴露出来；而**拼错表名**更糟——
    它会被当成"该表没启用"静默生效，是最难查的一类配置 bug。
    """
    # ★ 别把 dict 现有键并进 allowed —— 那样"未知表名"永远查不出来，
    #   校验变成永真式（本函数第一版就是这么写的，等于没校验）。
    allowed = {"draftpad", "dsh_compaction"}
    if not isinstance(search.rerank_tables, dict):
        raise ValueError(
            f"search.rerank_tables 必须是「表→布尔」的映射，得到 "
            f"{type(search.rerank_tables).__name__}: {search.rerank_tables!r}")
    for k, v in search.rerank_tables.items():
        if k not in allowed:
            raise ValueError(f"search.rerank_tables 含未知表名：{k!r}（允许 draftpad/dsh_compaction）")
        if not isinstance(v, bool):
            raise ValueError(f"search.rerank_tables[{k!r}] 必须是布尔，得到 {v!r}")
    if not isinstance(search.rerank_doc_chars, dict):
        raise ValueError(
            f"search.rerank_doc_chars 必须是「表→整数」的映射，得到 "
            f"{type(search.rerank_doc_chars).__name__}: {search.rerank_doc_chars!r}")
    for k, v in search.rerank_doc_chars.items():
        if k not in allowed:
            raise ValueError(f"search.rerank_doc_chars 含未知表名：{k!r}")
        if not isinstance(v, int) or v <= 0:
            raise ValueError(f"search.rerank_doc_chars[{k!r}] 必须是正整数，得到 {v!r}")


def default_config_path() -> Path:
    """默认配置路径：项目根目录下的 config.yaml。"""
    return Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_FILENAME
