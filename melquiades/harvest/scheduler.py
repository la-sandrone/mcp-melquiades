"""
文件名: melquiades/harvest/scheduler.py
摘要: 定时收割 —— MCP 进程内 daemon 线程 + threading.Event 停止信号（**可急停**）。

设计依据:
  · 三触发之一（定时器，默认 60s）；另两个是「工具搭车」「MCP 启动扫」
  · stdio MCP 进程寿命 = 会话寿命 ⇒ 定时器只覆盖会话期间；
    会话结束后的压缩点由下次会话的启动扫补齐
  · 线程**绝不抛异常**（任何错误只记日志），否则会拖垮 MCP 主服务
  · GIL 不影响并行：zstd 解压与 DB I/O 均释放 GIL

用法:
    sched = HarvestScheduler(runner, interval_sec=60)
    sched.start()
    ...
    sched.stop()          # 请求停止并等待（带超时）
依赖: harvest/runner.py
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from melquiades.harvest.runner import HarvestRunner, HarvestStats

logger = logging.getLogger(__name__)


class HarvestScheduler:
    """后台定时收割线程（daemon，带停止信号）。"""

    def __init__(
        self,
        runner: HarvestRunner,
        interval_sec: int = 60,
        stop_wait_timeout_sec: float = 5.0,
    ) -> None:
        self._runner = runner
        self._interval = max(5, int(interval_sec))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._wait_timeout = float(stop_wait_timeout_sec)
        self._last: Optional[HarvestStats] = None

    # ── 生命周期 ──

    def start(self) -> bool:
        """启动线程。已启动则返回 False（幂等）。"""
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="dsh-harvest-timer",
            daemon=True,          # 主进程退出时自动回收，不阻塞 MCP 退出
        )
        self._thread.start()
        logger.info("HarvestScheduler started (interval=%ds)", self._interval)
        return True

    def stop(self, timeout: Optional[float] = None) -> bool:
        """
        请求停止并等待线程结束。

        返回: True = 线程已停止（或从未启动）。
        """
        self._stop.set()
        t = self._thread
        if t is None:
            return True
        t.join(timeout if timeout is not None else self._wait_timeout)
        alive = t.is_alive()
        if alive:
            logger.warning("HarvestScheduler thread still alive after stop timeout")
        else:
            logger.info("HarvestScheduler stopped")
        return not alive

    # ── 状态 ──

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def last_stats(self) -> Optional[HarvestStats]:
        """最近一轮的统计（供状态工具展示）。"""
        return self._last

    # ── 循环 ──

    def _loop(self) -> None:
        """可中断睡眠 + 单轮收割。异常一律吞掉并记日志（绝不冒泡）。"""
        while not self._stop.is_set():
            # 可中断等待：stop 后立即醒来（等价 C 的 WaitForSingleObject 超时）
            if self._stop.wait(self._interval):
                break
            try:
                stats = self._runner.run_once("timer", stop_event=self._stop)
                self._last = stats
            except Exception as exc:  # noqa: BLE001 —— 有意的兜底
                logger.error("harvest timer cycle failed: %s: %s", type(exc).__name__, exc)
        logger.debug("harvest timer loop exited")


def spawn_fire_and_forget(fn, *args, **kwargs) -> threading.Thread:
    """
    后台线程执行一次性任务（工具搭车用）—— **触发即返回**，不阻塞工具响应。

    任何异常只记日志。
    """
    def _run() -> None:
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.error("fire-and-forget task failed: %s: %s", type(exc).__name__, exc)

    t = threading.Thread(target=_run, name="dsh-harvest-piggyback", daemon=True)
    t.start()
    return t
