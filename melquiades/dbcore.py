"""
文件名: melquiades/dbcore.py
摘要: MariaDB **即弃连接**核心（铁律一）—— 每次操作 connect → execute → close，
      **不使用连接池**。

      理由（血教训）：长连接出错后捞不回来，且会占满连接池。
      如个别路径确需池化，必须显式在此文件记录理由（当前无）。

      对外只暴露语义化方法（query / query_one / scalar / execute / transaction），
      让调用方**不需要**碰连接对象，从结构上保证"即弃"不被绕过。

依赖: mariadb
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional, Sequence

import mariadb

from melquiades.config import MariadbConfig

logger = logging.getLogger(__name__)


class MariaDB:
    """
    即弃连接访问器。

    用法:
        db = MariaDB(cfg.mariadb)
        rows = db.query("SELECT ... WHERE id=?", (1,))
        n, new_id = db.execute("INSERT INTO ... VALUES (?)", (x,))
    """

    def __init__(self, cfg: MariadbConfig) -> None:
        self._cfg = cfg

    # ── 连接 ──

    #: **建立连接阶段**值得重试的 errno 白名单。
    #: 全都是"服务器/网络的瞬时状态"，且此刻**尚未发出任何语句 ⇒ 重试幂等**。
    #:   110 ETIMEDOUT       SYN 被丢（本机实测主因，见 config 注释与 id=1092）
    #:   111 ECONNREFUSED    服务端未监听 / 正在重启
    #:   1040 连接数满       等一会儿就有位置
    #:   1043 握手被中断
    #:   2002/2003/2006/2013 C 客户端与连接器层的"连不上/连接丢失"
    RETRIABLE_CONNECT_ERRNOS: frozenset[int] = frozenset(
        {110, 111, 1040, 1043, 2002, 2003, 2006, 2013})

    @classmethod
    def _retriable_connect_error(cls, exc: BaseException) -> bool:
        """该连接异常是否值得重试。

        errno 在白名单外 ⇒ **不重试**：如 1045 Access denied、1049 Unknown database
        是配置错，重试三次只是把同一个错误报三遍（白等 + 噪声）。
        errno 缺失（连接器未填该字段）⇒ 按瞬时处理，但受"重试次数"与
        "总耗时预算"双重设限，不会无限拖。
        """
        errno = getattr(exc, "errno", None)
        if errno is None:
            return True
        try:
            return int(errno) in cls.RETRIABLE_CONNECT_ERRNOS
        except (TypeError, ValueError):
            return True

    def _connect(self) -> mariadb.Connection:
        """新建一个连接。**调用方负责关闭**（或使用下面的封装）。

        建立连接阶段失败会**重试**（次数与总预算见 config）——理由：
        本机偶发 SYN 丢包，TCP 重传节拍 1/3/7/15s，单次失败未必是真故障；
        而重试在此阶段是幂等的（一条语句都还没发）。
        ★ 查询**途中**失败绝不自动重试——写操作重放会双插，且无法判断
          服务端是否已执行。需要重试语义请在业务层显式实现。
        """
        c = self._cfg
        attempts = max(1, int(c.connect_retries) + 1)
        budget = float(c.connect_retry_budget_sec)
        t0 = time.monotonic()
        last_exc: Optional[BaseException] = None
        for i in range(attempts):
            try:
                # 注意（2026-09-22 实测）：mariadb 连接器**没有** charset 参数，
                # 字符集须通过 init_command="SET NAMES ..." 设置。
                return mariadb.connect(
                    host=c.host,
                    port=int(c.port),
                    database=c.database,
                    user=c.user,
                    password=c.password,
                    connect_timeout=int(c.connect_timeout),
                    read_timeout=int(c.read_timeout),
                    write_timeout=int(c.write_timeout),
                    autocommit=True,
                    init_command=f"SET NAMES {c.charset}",
                )
            except Exception as exc:  # noqa: BLE001 —— 分类后再决定重试或上浮
                last_exc = exc
                if not self._retriable_connect_error(exc):
                    raise
                elapsed = time.monotonic() - t0
                wait = float(c.connect_retry_backoff_sec) * (i + 1)
                if i + 1 >= attempts or elapsed + wait > budget:
                    break
                logger.warning(
                    "DB 连接失败（第 %d/%d 次，已耗时 %.1fs），%.1fs 后重试：%s",
                    i + 1, attempts, elapsed, wait, exc)
                time.sleep(wait)
        logger.error("DB 连接连续失败 %d 次（累计 %.1fs，预算 %.0fs），放弃：%s",
                     attempts, time.monotonic() - t0, budget, last_exc)
        assert last_exc is not None
        raise last_exc

    @contextmanager
    def cursor(self, dictionary: bool = True, readonly: bool = False) -> Iterator[Any]:
        """
        即弃游标：用完必定 close 连接（含异常路径）。

        readonly=True 时先 `SET SESSION TRANSACTION READ ONLY` —— **最硬的一层防线**：
        即使 SQL 校验被绕过、注入成功，该会话也**写不进去**
        （DROP/INSERT/UPDATE/DELETE 全部被服务器拒绝）。
        """
        conn = self._connect()
        try:
            if readonly:
                boot = conn.cursor()
                try:
                    boot.execute("SET SESSION TRANSACTION READ ONLY")
                finally:
                    boot.close()
            cur = conn.cursor(dictionary=dictionary)
            try:
                yield cur
            finally:
                cur.close()
        finally:
            conn.close()

    # ── 读 ──

    def query(
        self, sql: str, params: Sequence[Any] | None = None, readonly: bool = False
    ) -> list[dict]:
        """返回字典列表（无结果为 []）。readonly=True ⇒ 会话级只读。"""
        with self.cursor(dictionary=True, readonly=readonly) as cur:
            cur.execute(sql, tuple(params) if params else ())
            rows = cur.fetchall()
            return list(rows) if rows else []

    def query_one(
        self, sql: str, params: Sequence[Any] | None = None, readonly: bool = False
    ) -> Optional[dict]:
        """返回首行或 None。"""
        rows = self.query(sql, params, readonly=readonly)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        """返回首行首列或 None。"""
        with self.cursor(dictionary=False) as cur:
            cur.execute(sql, tuple(params) if params else ())
            row = cur.fetchone()
            return row[0] if row else None

    # ── 写 ──

    def execute(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> tuple[int, int]:
        """
        执行单条写语句。

        返回: (affected_rows, lastrowid)
        """
        with self.cursor(dictionary=False) as cur:
            cur.execute(sql, tuple(params) if params else ())
            return int(cur.rowcount or 0), int(cur.lastrowid or 0)

    def execute_many(self, sql: str, seq_params: Sequence[Sequence[Any]]) -> int:
        """批量执行同一条语句。返回受影响行数。"""
        with self.cursor(dictionary=False) as cur:
            cur.executemany(sql, [tuple(p) for p in seq_params])
            return int(cur.rowcount or 0)

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """
        显式事务（用于多条语句必须原子完成的场景）。

        注意：默认 autocommit=True，此处在**同一个连接**内手动 begin/commit，
        仍遵守"用完即弃"（连接在 finally 里关闭）。
        """
        conn = self._connect()
        try:
            conn.autocommit = False
            cur = conn.cursor(dictionary=True)
            try:
                yield cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()
        finally:
            conn.close()

    # ── 便捷 ──

    def table_exists(self, table: str) -> bool:
        """检查当前库中是否存在某表。"""
        row = self.query_one(
            "SELECT 1 AS ok FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = ?",
            (table,),
        )
        return row is not None

    def column_exists(self, table: str, column: str) -> bool:
        """检查某表是否存在某列。"""
        row = self.query_one(
            "SELECT 1 AS ok FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = ? AND column_name = ?",
            (table, column),
        )
        return row is not None

    def index_exists(self, table: str, index: str) -> bool:
        """检查某索引是否存在。"""
        row = self.query_one(
            "SELECT 1 AS ok FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = ? AND index_name = ?",
            (table, index),
        )
        return row is not None

    def ping(self) -> tuple[bool, str]:
        """连通性自检。返回 (ok, message)。"""
        try:
            v = self.scalar("SELECT VERSION()")
            return True, str(v)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    # ── 权限自检（安全姿态可观测）──

    #: 应用**不需要**的权限：留在账上就意味着"注入若成功可造成破坏"。
    #: 代码实际只需要 SELECT/INSERT/UPDATE/DELETE/CREATE/ALTER/INDEX。
    RISKY_PRIVILEGES: tuple[str, ...] = (
        "DROP", "GRANT OPTION", "FILE", "SUPER", "SHUTDOWN", "PROCESS", "RELOAD",
    )

    def check_privileges(self, scope_db: str = "") -> dict:
        """
        检查当前用户在本库（含 *.*）上的**危险权限**。

        返回 {"grants": [...], "risky": ["DROP", ...]}。
        用于启动时告警：权限被放宽时能立刻发现，而不是等出事（不静默）。
        """
        target = scope_db or self._cfg.database
        grants: list[str] = []
        risky_found: set[str] = set()
        try:
            for row in self.query("SHOW GRANTS"):
                g = str(list(row.values())[0])
                grants.append(g)
                # 只关心作用在本库或全局的授权
                if f"`{target}`" not in g and "ON *.*" not in g:
                    continue
                up = g.upper()
                if "ALL PRIVILEGES" in up:
                    risky_found.update(self.RISKY_PRIVILEGES)
                    continue
                for p in self.RISKY_PRIVILEGES:
                    if p in up:
                        risky_found.add(p)
        except Exception as exc:
            return {"grants": [], "risky": [], "error": f"{type(exc).__name__}: {exc}"}
        return {"grants": grants, "risky": sorted(risky_found)}
