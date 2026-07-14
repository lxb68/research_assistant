"""识别可恢复异常，并以受限重试和降级建议处理代理故障。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

import requests


T = TypeVar("T")
AsyncOperation = Callable[[], Awaitable[T]]


@dataclass(slots=True)
class RecoveryDecision:
    """记录异常分类、是否可恢复以及建议动作。"""
    category: str
    recoverable: bool
    action: str
    user_message: str


class RecoveryExhaustedError(RuntimeError):
    """表示安全重试次数已耗尽，并携带完整恢复轨迹。"""
    def __init__(self, message: str, *, decision: RecoveryDecision, trace: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.decision = decision
        self.trace = trace


class ErrorRecoveryAgent:
    """只执行安全重试和降级建议，不修改密钥、文件或业务数据。"""

    def __init__(
        self,
        *,
        max_cycles: int = 3,
        base_delay_seconds: float = 0.5,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.max_cycles = max(1, min(max_cycles, 8))
        self.base_delay_seconds = max(0.0, min(base_delay_seconds, 10.0))
        self.log_callback = log_callback

    async def execute(self, operation_name: str, operation: AsyncOperation[T]) -> tuple[T, list[dict[str, Any]]]:
        """执行异步操作，并仅对明确可恢复的错误进行有限重试。"""
        trace: list[dict[str, Any]] = []
        for cycle in range(1, self.max_cycles + 1):
            try:
                value = await operation()
                trace.append({"cycle": cycle, "status": "success", "operation": operation_name})
                return value, trace
            except Exception as error:
                decision = self.classify(error)
                trace.append(
                    {
                        "cycle": cycle,
                        "status": "failed",
                        "operation": operation_name,
                        "category": decision.category,
                        "recoverable": decision.recoverable,
                        "action": decision.action,
                        "message": self._safe_error_message(error),
                    }
                )
                self._log(
                    f"{operation_name} 第 {cycle}/{self.max_cycles} 次执行失败："
                    f"{decision.category}，{decision.action}"
                )
                if not decision.recoverable or cycle >= self.max_cycles:
                    raise RecoveryExhaustedError(
                        decision.user_message,
                        decision=decision,
                        trace=trace,
                    ) from error
                delay = min(self.base_delay_seconds * (2 ** (cycle - 1)), 4.0)
                if delay:
                    await asyncio.sleep(delay)
        raise AssertionError("错误恢复循环不应执行到此处")

    def classify(self, error: Exception) -> RecoveryDecision:
        """按 HTTP 状态和异常类型判断安全恢复策略。"""
        message = str(error).lower()
        status_code = self._status_code(error)
        if isinstance(error, (requests.Timeout, TimeoutError, asyncio.TimeoutError)) or "timeout" in message or "超时" in message:
            return RecoveryDecision("timeout", True, "等待后重试", "服务响应超时，已达到最大重试次数。")
        if isinstance(error, requests.ConnectionError) or any(
            token in message for token in ("connection reset", "connection refused", "network", "连接失败", "网络")
        ):
            return RecoveryDecision("network", True, "重新建立连接并重试", "网络连接失败，请检查后端网络后重试。")
        if status_code == 429 or "rate limit" in message or "too many requests" in message:
            return RecoveryDecision("rate_limit", True, "退避等待后重试", "模型或检索服务请求过于频繁，请稍后重试。")
        if status_code is not None and 500 <= status_code <= 599:
            return RecoveryDecision("upstream_5xx", True, "重试上游服务", "上游服务持续异常，请稍后重试。")
        if status_code in {401, 403} or any(token in message for token in ("unauthorized", "forbidden", "invalid api key")):
            return RecoveryDecision("authentication", False, "停止并请求用户检查配置", "服务鉴权失败，请在设置页面检查 API Key 和 Base URL。")
        if any(token in message for token in ("请先配置模型参数", "api key", "base url", "模型配置")):
            return RecoveryDecision("configuration", False, "停止并请求用户完成配置", "模型配置不完整，请先在设置页面完成配置。")
        if isinstance(error, (FileNotFoundError, PermissionError)):
            return RecoveryDecision("local_file", False, "停止并请求用户检查文件", "所需 PDF 或 Markdown 文件不存在或不可读，请重新导入材料。")
        if isinstance(error, ValueError):
            return RecoveryDecision("validation", False, "停止并返回参数提示", str(error))
        return RecoveryDecision("unknown", False, "停止，保留错误轨迹供排查", "流程遇到无法自动恢复的错误，请检查编排轨迹。")

    def _status_code(self, error: Exception) -> int | None:
        response = getattr(error, "response", None)
        value = getattr(response, "status_code", None)
        return int(value) if isinstance(value, int) else None

    def _safe_error_message(self, error: Exception) -> str:
        message = str(error).strip()
        lowered = message.lower()
        if any(token in lowered for token in ("api key", "authorization", "bearer ", "token=")):
            return "错误信息包含敏感配置，已隐藏"
        return message[:500] or error.__class__.__name__

    def _log(self, message: str) -> None:
        if self.log_callback:
            self.log_callback(message)


__all__ = ["ErrorRecoveryAgent", "RecoveryDecision", "RecoveryExhaustedError"]
