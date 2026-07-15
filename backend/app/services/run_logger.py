"""将每次代理运行同时记录为人类可读日志和结构化 JSONL。"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RunLogger:
    """为一次 Agent 编排运行写入可读日志和 JSONL 结构化日志。"""

    def __init__(self, root_dir: str | Path, *, run_id: str | None = None) -> None:
        """初始化当前对象所需的配置与运行状态。"""
        self.run_id = run_id or uuid.uuid4().hex
        day = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        self.run_dir = Path(root_dir) / day
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.text_path = self.run_dir / f"{self.run_id}.log"
        self.jsonl_path = self.run_dir / f"{self.run_id}.jsonl"
        self._lock = threading.Lock()

    def log(self, component: str, message: str, *, event: str = "log", data: dict[str, Any] | None = None) -> None:
        """写入脱敏后的人类可读日志和结构化日志。"""
        timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
        safe_message = self._redact(str(message))
        safe_data = self._redact_value(data or {})
        record = {
            "timestamp": timestamp,
            "runId": self.run_id,
            "component": component,
            "event": event,
            "message": safe_message,
            "data": safe_data,
        }
        text_line = f"{timestamp} [{self.run_id}] [{component}] [{event}] {safe_message}"
        if safe_data:
            text_line += f" | {json.dumps(safe_data, ensure_ascii=False, default=str)}"
        with self._lock:
            with self.text_path.open("a", encoding="utf-8") as handle:
                handle.write(text_line + "\n")
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def public_info(self) -> dict[str, str]:
        """返回可安全公开的运行标识与日志路径。"""
        return {"runId": self.run_id, "logPath": str(self.text_path), "jsonlPath": str(self.jsonl_path)}

    def _redact(self, value: str) -> str:
        """递归脱敏日志消息中的凭据和敏感字段。"""
        patterns = (
            (r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1***"),
            (r"(?i)((?:api[_ -]?key|token|secret)\s*[:=]\s*)[^\s,;]+", r"\1***"),
            (r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-***"),
        )
        redacted = value
        for pattern, replacement in patterns:
            redacted = re.sub(pattern, replacement, redacted)
        return redacted

    def _redact_value(self, value: Any) -> Any:
        """对单个字符串值执行凭据模式脱敏。"""
        if isinstance(value, dict):
            return {
                str(key): "***" if self._is_sensitive_key(str(key)) else self._redact_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        if isinstance(value, str):
            return self._redact(value)
        return value

    def _is_sensitive_key(self, key: str) -> bool:
        """只匹配凭据字段，避免误伤 tokenCount、searchKeyword 等诊断字段。"""
        normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
        return (
            "authorization" in normalized
            or "apikey" in normalized
            or "secret" in normalized
            or normalized.endswith("token")
        )


__all__ = ["RunLogger"]
