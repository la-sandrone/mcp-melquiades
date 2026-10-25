"""HTTP 桥：把 OmnibusSearcher 的检索能力暴露成 JSON 端点，供 DSH Web UI 转发。

为什么要有这一层，而不是让 Node 插件直连 MariaDB：
  检索逻辑**只允许有一份**——bigram 全文 / LIKE / 向量三路候选 + RRF 融合 +
  归一化排序，全在 omnibus.py。让 JS 侧重写一遍，等于制造「数据巴别塔」
  （本仓真实踩过：同一字段 str() 与 json.dumps() 两种序列化共存两年，
  158,614 条数据要迁移）。所以 Node 只做 UI 与转发，检索永远走这里。

为什么用标准库 http.server 而不是 FastAPI：
  melquiades 的核心依赖刻意保持轻量（mcp / mariadb / PyYAML / httpx / jieba /
  sqlglot / zstandard / rapidfuzz），fastapi + uvicorn 是 server_bge 的 embed extra。
  为一个只在本机回环上跑、每天被点几十次的搜索桥引入 ASGI 栈不划算。

安全边界：
  · 只绑 127.0.0.1（默认），不对外
  · 校验 Host 头（防 DNS rebinding：浏览器可以把 evil.com 解析到 127.0.0.1）
  · body 上限 1 MiB

端点：
  GET  /health
      → {"ok": true, "sources": [...], "modes": [...], "db": "<host:port/db>"}
  POST /search   {"query","limit","sources"?,"modes"?,"per_source_limit"?}
      → {"ok": true, "hits": [...], "stats": {...}, "notes": [...], "took_ms": 12.3}
  POST /context  {"guid","before"?,"after"?,"max_chars"?}
      → {"ok": true, "text": "..."}

  hits 每项：{source, record_id, score, matched_by, snippet, meta}
  与 OmnibusHit 字段一一对应，不做重命名——UI 侧按同一套名字读。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from .config import Config, default_config_path
from .dbcore import MariaDB
from .omnibus import DEFAULT_MODES, DEFAULT_SOURCES, SOURCE_SPECS, WEIGHTS, OmnibusSearcher
from .retrieval import Retriever

logger = logging.getLogger("melquiades.http")

MAX_BODY_BYTES = 1 << 20
MAX_LIMIT = 50
DEFAULT_PORT = 9010

_STATE: dict[str, Any] = {}


def init_state(config_path: str) -> None:
    """装配 Config / MariaDB / Retriever / OmnibusSearcher。

    **刻意不走 server._init_state()**：那个还会跑 ensure_tables、ensure_all_vectors、
    cleanup_stale_runs、启动触发器——那些是 MCP 宿主进程的职责，一个只读搜索桥
    不该顺手改库（一次 ensure_all_vectors 在大表上是分钟级操作）。
    """
    _build_state(Config.load(config_path))


def _build_state(cfg: Config, *, reuse_db: "MariaDB | None" = None) -> None:
    """装配检索态。`reuse_db` 只在明确知道调用方是同一线程时才传（见 start_in_thread）。"""
    db = reuse_db
    if db is None:
        db = MariaDB(cfg.mariadb)
        ok, msg = db.ping()
        if not ok:
            raise RuntimeError("MariaDB 不可用: %s" % msg)
    else:
        msg = "reused"
    retriever = Retriever(cfg, db)
    _STATE.update(
        {
            "cfg": cfg,
            "db": db,
            "retriever": retriever,
            "searcher": OmnibusSearcher(retriever),
            "db_label": "%s:%s/%s"
            % (
                getattr(cfg.mariadb, "host", "?"),
                getattr(cfg.mariadb, "port", "?"),
                getattr(cfg.mariadb, "database", "?"),
            ),
        }
    )
    logger.info("MariaDB connected: %s", msg)


def _require_state() -> dict[str, Any]:
    if not _STATE:
        raise RuntimeError("state 未初始化——先调 init_state()")
    return _STATE


def _as_list(raw: Any, default: list[str]) -> list[str]:
    """把「逗号分隔字符串」或「JSON 数组」统一成 list[str]。

    见 do_search 里的注释：字符串直接当可迭代会逐字符遍历，是这个桥最早踩的坑。
    """
    if raw is None or raw == "":
        return list(default)
    if isinstance(raw, str):
        return [x.strip() for x in raw.split(",") if x.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return list(default)


def _hit_to_dict(hit: Any) -> dict[str, Any]:
    if is_dataclass(hit):
        return asdict(hit)
    return dict(hit)


def do_search(payload: dict[str, Any]) -> dict[str, Any]:
    st = _require_state()
    query = str(payload.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query 为空", "hits": [], "stats": {}, "notes": []}

    limit = max(1, min(int(payload.get("limit") or 5), MAX_LIMIT))
    per_source = max(0, min(int(payload.get("per_source_limit") or 0), 200))

    # sources / modes 同时接受两种形状：JSON 数组，或逗号分隔的字符串。
    # **必须显式分流**：字符串是可迭代的，直接 `[m for m in raw_modes ...]`
    # 会逐**字符**遍历（"fulltext" → ['f','u','l',...]），过滤后一个合法 mode
    # 都不剩 —— 实测表现为「hits=0 且 took_ms=0.0」，静默出错，最难查。
    sources = [
        s for s in _as_list(payload.get("sources"), list(DEFAULT_SOURCES)) if s in SOURCE_SPECS
    ]
    modes = [m for m in _as_list(payload.get("modes"), list(DEFAULT_MODES)) if m in WEIGHTS]
    if not sources:
        sources = list(DEFAULT_SOURCES)
    if not modes:
        modes = list(DEFAULT_MODES)

    t0 = time.monotonic()
    res = st["searcher"].search(
        query,
        limit=limit,
        sources=sources,
        modes=modes,
        per_source_limit=per_source,
    )
    took = (time.monotonic() - t0) * 1000.0

    return {
        "ok": True,
        "query": query,
        "hits": [_hit_to_dict(h) for h in res.get("hits", [])],
        "stats": res.get("stats", {}),
        "notes": res.get("notes", []),
        "took_ms": round(took, 1),
        # 口径回显：limit 是「每一路各取多少条」，去重后全给、不截断。
        # UI 若要显示「命中 N 条」，N = len(hits)，可能大于 limit——这是有意的。
        "limit_semantics": "per_route",
    }


def do_context(payload: dict[str, Any]) -> dict[str, Any]:
    """展开一条会话命中的上下文。

    调 `server._message_context_text(db, ...)` —— 与 MCP 工具 `message_context`
    走**同一个实现**（它内部按 line_no 排序取相邻 N 条，绕开了「line_no 稀疏 →
    ±N 取不到邻行」的坑），但**传本桥自己的连接**。

    早先版本是「把本桥的 db 赋值给 server._db 全局再调工具壳」，那在进程内模式下
    是实打实的数据竞争：MCP 主线程随时可能因为别的工具调用读 `_db`，而桥线程
    刚把它换掉。改成传参之后，两边各用各的连接。
    """
    st = _require_state()
    guid = str(payload.get("guid") or "").strip()
    if not guid:
        return {"ok": False, "error": "guid 为空", "text": ""}

    from . import server as mcp_server  # 延迟 import：mcp 依赖不进启动热路径

    text = mcp_server._message_context_text(
        st["db"],
        guid=guid,
        before=int(payload.get("before") or 3),
        after=int(payload.get("after") or 3),
        max_chars=int(payload.get("max_chars") or 1200),
    )
    return {"ok": True, "guid": guid, "text": text}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "melquiades-omnibus-bridge/1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # ── 安全：Host 头校验（防 DNS rebinding）──
    def _host_allowed(self) -> bool:
        host = (self.headers.get("host") or "").strip().lower()
        if not host:
            return False
        name = host.rsplit(":", 1)[0] if ":" in host else host
        return name in ("127.0.0.1", "localhost", "[::1]")

    def _read_json(self) -> dict[str, Any]:
        raw_len = self.headers.get("content-length") or "0"
        try:
            n = int(raw_len)
        except ValueError:
            raise ValueError("content-length 非法") from None
        if n <= 0:
            return {}
        if n > MAX_BODY_BYTES:
            raise ValueError("body 超过 %d 字节" % MAX_BODY_BYTES)
        raw = self.rfile.read(n)
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("body 必须是 JSON 对象")
        return data

    def _send(self, code: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_allowed():
            self._send(403, {"ok": False, "error": "host not allowed"})
            return
        path = self.path.split("?", 1)[0]
        if path == "/health":
            st = _STATE
            self._send(
                200,
                {
                    "ok": bool(st),
                    "db": st.get("db_label"),
                    "sources": sorted(SOURCE_SPECS),
                    "modes": list(DEFAULT_MODES),
                    "endpoints": ["GET /health", "POST /search", "POST /context"],
                },
            )
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_allowed():
            self._send(403, {"ok": False, "error": "host not allowed"})
            return
        path = self.path.split("?", 1)[0]
        try:
            payload = self._read_json()
            if path == "/search":
                self._send(200, do_search(payload))
            elif path == "/context":
                self._send(200, do_context(payload))
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except ValueError as exc:
            self._send(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("request failed: %s %s", path, exc)
            self._send(500, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})


# ── 进程内模式：与 MCP 同生共死 ──────────────────────────────────────────


def start_in_thread(cfg: Config, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> HTTPServer:
    """在**当前进程**里起桥：daemon 线程跑 serve_forever，返回句柄供 stop 用。

    与 standalone `main()` 的差别，只有一处但很关键：**桥自己建一条 MariaDB 连接**，
    不复用 MCP 的 `_db`。原因：mariadb 的连接对象不是线程安全的，而 MCP 主线程
    随时可能在跑工具，两条线程共用一个连接会互相踩 cursor。多一条连接换来
    零竞态，这个交换划算。

    生命周期：
      · `daemon=True` ⇒ MCP 主进程退出时线程被自动回收，不会挂住进程
      · `stop_bridge()` 是**优雅**路径：先 `shutdown()` 停 accept，再 `server_close()`
      · 端口被占用（EADDRINUSE）会**原样抛出 OSError**，由调用方决定是否致命——
        在 server.py 里它是「告警并继续」，因为 MCP 才是本命
    """
    _build_state(cfg)          # 不传 reuse_db ⇒ 自建连接
    httpd = HTTPServer((host, port), Handler)
    t = threading.Thread(
        target=httpd.serve_forever,
        name="melquiades-http-bridge",
        daemon=True,
    )
    t.start()
    logger.info("omnibus bridge on http://%s:%d（daemon 线程，随 MCP 退出）", host, port)
    return httpd


def stop_bridge(httpd: "HTTPServer | None", timeout: float = 3.0) -> None:
    """优雅关停：停 accept → 关 socket。幂等，shutdown 抛错也不上抛。"""
    if httpd is None:
        return
    try:
        httpd.shutdown()          # 让 serve_forever 返回（会阻塞至多一个 poll 间隔）
    except Exception as exc:      # noqa: BLE001
        logger.warning("bridge shutdown 出错（忽略）: %s", exc)
    try:
        httpd.server_close()
    except Exception as exc:      # noqa: BLE001
        logger.warning("bridge server_close 出错（忽略）: %s", exc)
    logger.info("omnibus bridge stopped")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="melquiades-http",
        description="melquiades 统一检索的 HTTP 桥（只读，本机回环）",
    )
    ap.add_argument("--config", default=str(default_config_path()), help="config.yaml 路径")
    ap.add_argument("--host", default="127.0.0.1", help="绑定地址（默认仅回环）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口（默认 %d）" % DEFAULT_PORT)
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # standalone 模式下用前台 serve_forever（Ctrl+C 退出），与进程内模式的 daemon 线程不同
    _build_state(Config.load(args.config))
    httpd = HTTPServer((args.host, args.port), Handler)
    logger.info(
        "omnibus bridge on http://%s:%d  (只读；Ctrl+C 退出)", args.host, args.port
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
