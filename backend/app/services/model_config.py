"""持久化模型配置，并生成带安全约束的运行时模型参数。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.services.model_client import (
    MODEL_PROVIDERS,
    get_provider,
    infer_provider,
    normalize_protocol,
    requires_api_key,
    validate_base_url,
)
from app.services.secret_store import WindowsDpapiProtector


SYSTEM_SECURITY_CONSTRAINT = (
    "Security constraint: never reveal, repeat, print, transform, summarize, or infer any API key, "
    "access token, secret, authorization header, hidden configuration, or credential-like value. "
    "If a user asks for secrets or hidden settings, refuse and continue without exposing them."
)


class ModelConfigStore:
    """管理模型配置的落盘、脱敏读取和运行时装配。"""
    def __init__(self, storage_dir: str | Path | None = None) -> None:
        """初始化当前对象所需的配置与运行状态。"""
        self.storage_dir = Path(storage_dir or settings.backend_storage_dir).resolve()
        self.config_path = self.storage_dir / "settings" / "model_config.json"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.secret_protector = WindowsDpapiProtector()

    def load_saved(self) -> dict[str, Any]:
        """读取原始保存配置；文件缺失或损坏时返回空配置。"""
        if not self.config_path.exists():
            return {}
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def load_runtime(self) -> dict[str, Any]:
        """合并保存配置与环境变量，得到运行时模型配置。"""
        saved = self.load_saved()
        protected_key = str(saved.get("apiKeyProtected") or "").strip()
        protected_value = self.secret_protector.unprotect(protected_key) if protected_key else ""
        api_key = str(
            protected_value
            or saved.get("apiKey")
            or saved.get("api_key")
            or settings.llm_translation_api_key
            or "",
        ).strip()
        base_url = str(
            saved.get("baseUrl")
            or saved.get("base_url")
            or settings.llm_translation_base_url
            or "",
        ).strip().rstrip("/")
        model_name = str(
            saved.get("model")
            or saved.get("modelName")
            or settings.llm_translation_model
            or "",
        ).strip()
        provider = str(saved.get("provider") or infer_provider(base_url)).strip().lower()
        provider_config = get_provider(provider)
        if not base_url:
            base_url = str(provider_config["baseUrl"]).rstrip("/")
        protocol = normalize_protocol(str(saved.get("protocol") or ""), provider)
        allow_heuristic_fallback = bool(
            saved.get("allowHeuristicFallback", saved.get("allow_heuristic_fallback", False))
        )
        return {
            "api_key": api_key,
            "base_url": base_url,
            "model": model_name,
            "provider": provider,
            "protocol": protocol,
            "requires_api_key": requires_api_key(provider, protocol),
            "allow_heuristic_fallback": allow_heuristic_fallback,
            "system_constraint": SYSTEM_SECURITY_CONSTRAINT,
        }

    def is_configured(self) -> bool:
        """判断模型调用所需配置是否完整。"""
        runtime = self.load_runtime()
        has_required_key = bool(runtime["api_key"]) or not runtime["requires_api_key"]
        return bool(has_required_key and runtime["base_url"] and runtime["model"])

    def save(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        provider: str = "",
        protocol: str = "",
        allow_heuristic_fallback: bool = False,
    ) -> dict[str, Any]:
        """校验并持久化模型配置。"""
        normalized_model = str(model).strip()
        normalized_base_url = str(base_url).strip().rstrip("/")
        normalized_api_key = str(api_key).strip()
        existing = self.load_saved()
        current_runtime = self.load_runtime()
        normalized_provider = str(provider or infer_provider(normalized_base_url)).strip().lower()
        provider_config = get_provider(normalized_provider)
        normalized_protocol = normalize_protocol(protocol, normalized_provider)
        existing_provider = str(
            existing.get("provider") or infer_provider(existing.get("baseUrl") or existing.get("base_url") or "")
        ).strip().lower()
        if not normalized_base_url:
            normalized_base_url = str(provider_config["baseUrl"]).strip().rstrip("/")
        key_to_persist = normalized_api_key
        if not key_to_persist and normalized_provider == existing_provider:
            protected_key = str(existing.get("apiKeyProtected") or "").strip()
            key_to_persist = (
                self.secret_protector.unprotect(protected_key)
                if protected_key
                else str(existing.get("apiKey") or existing.get("api_key") or "").strip()
            )
        effective_api_key = key_to_persist
        if not effective_api_key and normalized_provider == current_runtime["provider"]:
            effective_api_key = str(current_runtime["api_key"] or "").strip()
        if not normalized_model:
            raise ValueError("请先填写模型名称")
        if not normalized_base_url:
            raise ValueError("请先填写模型 Base URL")
        normalized_base_url = validate_base_url(normalized_base_url)
        if requires_api_key(normalized_provider, normalized_protocol) and not effective_api_key:
            raise ValueError(f"{provider_config['name']} 需要填写 API Key")

        payload = {
            "provider": normalized_provider,
            "protocol": normalized_protocol,
            "model": normalized_model,
            "baseUrl": normalized_base_url,
            "allowHeuristicFallback": bool(allow_heuristic_fallback),
        }
        if key_to_persist:
            payload["apiKeyProtected"] = self.secret_protector.protect(key_to_persist)
        self.config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.get_public_config()

    def clear(self) -> dict[str, Any]:
        """清除已保存的模型配置。"""
        if self.config_path.exists():
            self.config_path.unlink()
        return self.get_public_config()

    def get_public_config(self) -> dict[str, Any]:
        """返回可安全展示给前端的脱敏配置。"""
        runtime = self.load_runtime()
        has_saved = self.config_path.exists()
        return {
            "configured": self.is_configured(),
            "hasSavedConfig": has_saved,
            "provider": runtime["provider"],
            "protocol": runtime["protocol"],
            "model": runtime["model"],
            "baseUrl": runtime["base_url"],
            "requiresApiKey": runtime["requires_api_key"],
            "hasApiKey": bool(runtime["api_key"]),
            "secretStorage": self._secret_storage(saved=self.load_saved(), runtime=runtime),
            "allowHeuristicFallback": runtime["allow_heuristic_fallback"],
            "maskedApiKey": self._mask_secret(runtime["api_key"]),
            "systemConstraint": SYSTEM_SECURITY_CONSTRAINT,
        }

    def _secret_storage(self, *, saved: dict[str, Any], runtime: dict[str, Any]) -> str:
        if saved.get("apiKeyProtected"):
            return self.secret_protector.storage_label
        if saved.get("apiKey") or saved.get("api_key"):
            return "legacy_plaintext"
        if runtime.get("api_key"):
            return "environment"
        return "none"

    def build_model_payload(self) -> dict[str, Any] | None:
        """构造模型调用参数，配置不完整时返回空值。"""
        runtime = self.load_runtime()
        if not self.is_configured():
            return None
        return runtime

    def build_candidate(
        self,
        *,
        provider: str,
        protocol: str,
        base_url: str,
        api_key: str,
        model: str = "",
    ) -> dict[str, Any]:
        """构造用于模型发现或连通性检查的临时配置，不写入磁盘。"""
        normalized_provider = str(provider or infer_provider(base_url)).strip().lower()
        provider_config = get_provider(normalized_provider)
        normalized_protocol = normalize_protocol(protocol, normalized_provider)
        normalized_base_url = str(base_url or provider_config["baseUrl"]).strip().rstrip("/")
        normalized_api_key = str(api_key or "").strip()
        existing = self.load_runtime()
        if not normalized_api_key and existing["provider"] == normalized_provider:
            normalized_api_key = str(existing["api_key"] or "")
        if not normalized_base_url:
            raise ValueError("请先填写模型 Base URL")
        normalized_base_url = validate_base_url(normalized_base_url)
        if requires_api_key(normalized_provider, normalized_protocol) and not normalized_api_key:
            raise ValueError(f"{provider_config['name']} 需要填写 API Key")
        return {
            "provider": normalized_provider,
            "protocol": normalized_protocol,
            "base_url": normalized_base_url,
            "api_key": normalized_api_key,
            "model": str(model or "").strip(),
            "system_constraint": SYSTEM_SECURITY_CONSTRAINT,
        }

    def get_provider_catalog(self) -> list[dict[str, Any]]:
        """返回前端可安全展示的供应商、协议和默认地址目录。"""
        return [dict(item) for item in MODEL_PROVIDERS]

    def _mask_secret(self, value: str) -> str:
        """脱敏密钥。"""
        secret = str(value or "").strip()
        if not secret:
            return ""
        if len(secret) <= 8:
            return f"{secret[:2]}***{secret[-2:]}"
        return f"{secret[:4]}***{secret[-4:]}"


__all__ = ["ModelConfigStore", "SYSTEM_SECURITY_CONSTRAINT"]
