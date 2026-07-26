"""提供线程安全的不可变运行时配置快照与任务级绑定。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from threading import RLock
from typing import Any, Iterator, Mapping


@dataclass(frozen=True)
class RuntimeSettingsSnapshot:
    """一次完整且不可变的运行时配置版本。"""

    revision: int
    values: Mapping[str, Any] = field(repr=False)

    def get(self, name: str) -> Any:
        if name not in self.values:
            raise AttributeError(f"未知运行时配置：{name}")
        return self.values[name]


class RuntimeConfigManager:
    """构造候选快照并以单次引用替换提交配置。"""

    def __init__(self, initial_values: Mapping[str, Any], *, revision: int = 1) -> None:
        self._lock = RLock()
        self._snapshot = self._create_snapshot(revision, initial_values)

    @classmethod
    def from_object(cls, source: object) -> "RuntimeConfigManager":
        values = {
            name: deepcopy(value)
            for name, value in vars(type(source)).items()
            if not name.startswith("_")
            and not callable(value)
            and not isinstance(value, (classmethod, staticmethod, property))
        }
        return cls(values)

    @staticmethod
    def _create_snapshot(revision: int, values: Mapping[str, Any]) -> RuntimeSettingsSnapshot:
        def freeze(value: Any) -> Any:
            if isinstance(value, dict):
                return MappingProxyType({key: freeze(item) for key, item in value.items()})
            if isinstance(value, list):
                return tuple(freeze(item) for item in value)
            if isinstance(value, tuple):
                return tuple(freeze(item) for item in value)
            if isinstance(value, set):
                return frozenset(freeze(item) for item in value)
            return deepcopy(value)

        return RuntimeSettingsSnapshot(
            revision=max(1, int(revision)),
            values=MappingProxyType({key: freeze(value) for key, value in values.items()}),
        )

    def snapshot(self) -> RuntimeSettingsSnapshot:
        with self._lock:
            return self._snapshot

    def build_candidate(self, updates: Mapping[str, Any]) -> RuntimeSettingsSnapshot:
        with self._lock:
            values = dict(self._snapshot.values)
            values.update(deepcopy(dict(updates)))
            return self._create_snapshot(self._snapshot.revision + 1, values)

    def commit(self, candidate: RuntimeSettingsSnapshot) -> RuntimeSettingsSnapshot:
        with self._lock:
            if candidate.revision != self._snapshot.revision + 1:
                raise RuntimeError("运行时配置版本已变化，请重新提交")
            self._snapshot = candidate
            return self._snapshot

    def replace(self, updates: Mapping[str, Any]) -> RuntimeSettingsSnapshot:
        """供测试和内部单字段覆盖使用；业务更新应先持久化再提交候选。"""
        with self._lock:
            candidate = self.build_candidate(updates)
            return self.commit(candidate)


_bound_snapshot: ContextVar[RuntimeSettingsSnapshot | None] = ContextVar(
    "runtime_settings_snapshot",
    default=None,
)


@contextmanager
def bind_runtime_settings(snapshot: RuntimeSettingsSnapshot) -> Iterator[RuntimeSettingsSnapshot]:
    """让一次请求或任务在整个生命周期内固定使用同一配置版本。"""

    token = _bound_snapshot.set(snapshot)
    try:
        yield snapshot
    finally:
        _bound_snapshot.reset(token)


class RuntimeSettingsProxy:
    """保持 ``settings.xxx`` 兼容，同时从当前绑定快照读取值。"""

    __slots__ = ("_manager", "_overrides", "_override_lock")

    def __init__(self, manager: RuntimeConfigManager) -> None:
        object.__setattr__(self, "_manager", manager)
        object.__setattr__(self, "_overrides", {})
        object.__setattr__(self, "_override_lock", RLock())

    def snapshot(self) -> RuntimeSettingsSnapshot:
        return _bound_snapshot.get() or self._manager.snapshot()

    @property
    def revision(self) -> int:
        return self.snapshot().revision

    def __getattr__(self, name: str) -> Any:
        with self._override_lock:
            if name in self._overrides:
                return self._overrides[name]
        return self.snapshot().get(name)

    def __setattr__(self, name: str, value: Any) -> None:
        # unittest.mock.patch.object 依赖临时设置属性；覆盖值不能污染正式快照。
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        with self._override_lock:
            self._overrides[name] = value

    def __delattr__(self, name: str) -> None:
        with self._override_lock:
            if name in self._overrides:
                del self._overrides[name]
                return
        raise AttributeError(name)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(self._manager.snapshot().values))


__all__ = [
    "RuntimeConfigManager",
    "RuntimeSettingsProxy",
    "RuntimeSettingsSnapshot",
    "bind_runtime_settings",
]
