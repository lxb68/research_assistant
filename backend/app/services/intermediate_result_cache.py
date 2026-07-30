"""为研究管线提供带完整输入指纹的进程内中间结果缓存。"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections import OrderedDict
from typing import Any


class StrictIntermediateCache:
    """只在所有影响结果的输入完全一致时复用结果。"""

    def __init__(self, *, max_entries: int = 128) -> None:
        self.max_entries = max(1, int(max_entries))
        self._values: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def make_key(namespace: str, payload: Any) -> str:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return f"{namespace}:{digest}"

    def get(self, key: str) -> Any | None:
        with self._lock:
            if key not in self._values:
                return None
            value = self._values.pop(key)
            self._values[key] = value
            return copy.deepcopy(value)

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._values.pop(key, None)
            self._values[key] = copy.deepcopy(value)
            while len(self._values) > self.max_entries:
                self._values.popitem(last=False)

    def clear(self) -> None:
        """清空当前进程缓存，供配置刷新和隔离测试使用。"""
        with self._lock:
            self._values.clear()


__all__ = ["StrictIntermediateCache"]
