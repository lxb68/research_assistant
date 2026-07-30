"""为进程内所有模型请求提供统一的有界并发控制。"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class ModelCallLimiter:
    """使用共享信号量限制模型服务的进程级并发。"""

    def __init__(self, max_concurrency: int) -> None:
        self.max_concurrency = max(1, int(max_concurrency))
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)
        self._lock = threading.Lock()
        self._active = 0
        self._peak = 0

    @contextmanager
    def slot(self) -> Iterator[None]:
        """等待并占用一个模型调用名额，退出时可靠释放。"""
        self._semaphore.acquire()
        with self._lock:
            self._active += 1
            self._peak = max(self._peak, self._active)
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1
            self._semaphore.release()

    def snapshot(self) -> dict[str, int]:
        """返回不含敏感信息的并发诊断快照。"""
        with self._lock:
            return {
                "maxConcurrency": self.max_concurrency,
                "active": self._active,
                "peak": self._peak,
            }


__all__ = ["ModelCallLimiter"]
