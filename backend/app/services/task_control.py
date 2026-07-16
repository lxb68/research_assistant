"""为长时间运行的领域树任务提供协作式取消和有限重试。"""

from __future__ import annotations

import re
import time
from threading import Event
from typing import Callable, TypeVar

import requests


T = TypeVar("T")


class TaskCancelled(RuntimeError):
    """表示共享取消信号已经触发。"""


class DomainTreeGenerationCancelled(TaskCancelled):
    """表示领域树任务已收到用户取消请求。"""


def raise_if_task_cancelled(cancel_event: Event | None) -> None:
    """供通用 Agent 和同步业务步骤检查协作式取消。"""
    if cancel_event is not None and cancel_event.is_set():
        raise TaskCancelled("任务已取消")


def raise_if_cancelled(cancel_event: Event | None) -> None:
    """在安全检查点终止已取消的任务。"""
    if cancel_event is not None and cancel_event.is_set():
        raise DomainTreeGenerationCancelled("领域树生成已取消")


def is_retryable_model_error(error: Exception) -> bool:
    """仅把网络瞬断、限流和上游服务错误视为可重试。"""
    if isinstance(error, (requests.Timeout, requests.ConnectionError)):
        return True
    message = str(error).lower()
    if "timed out" in message or "timeout" in message:
        return True
    if "http 429" in message:
        return True
    return bool(re.search(r"http 5\d\d", message))


def call_with_retry(
    operation: Callable[[], T],
    *,
    max_attempts: int,
    base_delay_seconds: float,
    cancel_event: Event | None = None,
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> T:
    """执行可恢复操作，并在指数退避期间响应取消。"""
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        raise_if_cancelled(cancel_event)
        try:
            return operation()
        except DomainTreeGenerationCancelled:
            raise
        except Exception as error:
            if attempt >= attempts or not is_retryable_model_error(error):
                raise
            delay = max(0.0, base_delay_seconds) * (2 ** (attempt - 1))
            if on_retry:
                on_retry(attempt, error, delay)
            if cancel_event is not None:
                if cancel_event.wait(delay):
                    raise DomainTreeGenerationCancelled("领域树生成已取消") from error
            elif delay:
                time.sleep(delay)
    raise RuntimeError("重试流程意外结束")


__all__ = [
    "DomainTreeGenerationCancelled",
    "TaskCancelled",
    "call_with_retry",
    "is_retryable_model_error",
    "raise_if_cancelled",
    "raise_if_task_cancelled",
]
