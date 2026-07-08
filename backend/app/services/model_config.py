from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.config import settings


SYSTEM_SECURITY_CONSTRAINT = (
    "Security constraint: never reveal, repeat, print, transform, summarize, or infer any API key, "
    "access token, secret, authorization header, hidden configuration, or credential-like value. "
    "If a user asks for secrets or hidden settings, refuse and continue without exposing them."
)


class ModelConfigStore:
    def __init__(self, storage_dir: str | Path | None = None) -> None:
        self.storage_dir = Path(storage_dir or settings.backend_storage_dir).resolve()
        self.config_path = self.storage_dir / "settings" / "model_config.json"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

    def load_saved(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {}
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def load_runtime(self) -> dict[str, str]:
        saved = self.load_saved()
        api_key = str(
            saved.get("apiKey")
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
        return {
            "api_key": api_key,
            "base_url": base_url,
            "model": model_name,
            "system_constraint": SYSTEM_SECURITY_CONSTRAINT,
        }

    def is_configured(self) -> bool:
        runtime = self.load_runtime()
        return bool(runtime["api_key"] and runtime["base_url"] and runtime["model"])

    def save(self, *, model: str, base_url: str, api_key: str) -> dict[str, Any]:
        normalized_model = str(model).strip()
        normalized_base_url = str(base_url).strip().rstrip("/")
        normalized_api_key = str(api_key).strip()
        existing = self.load_saved()
        if not normalized_api_key:
            normalized_api_key = str(existing.get("apiKey") or existing.get("api_key") or "").strip()
        if not normalized_model:
            raise ValueError("请先填写模型名称")
        if not normalized_base_url:
            raise ValueError("请先填写模型 Base URL")
        if not normalized_api_key:
            raise ValueError("请先填写模型密钥")

        payload = {
            "model": normalized_model,
            "baseUrl": normalized_base_url,
            "apiKey": normalized_api_key,
        }
        self.config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.get_public_config()

    def clear(self) -> dict[str, Any]:
        if self.config_path.exists():
            self.config_path.unlink()
        return self.get_public_config()

    def get_public_config(self) -> dict[str, Any]:
        runtime = self.load_runtime()
        has_saved = self.config_path.exists()
        return {
            "configured": self.is_configured(),
            "hasSavedConfig": has_saved,
            "model": runtime["model"],
            "baseUrl": runtime["base_url"],
            "hasApiKey": bool(runtime["api_key"]),
            "maskedApiKey": self._mask_secret(runtime["api_key"]),
            "systemConstraint": SYSTEM_SECURITY_CONSTRAINT,
        }

    def build_model_payload(self) -> dict[str, str] | None:
        runtime = self.load_runtime()
        if not (runtime["api_key"] and runtime["base_url"] and runtime["model"]):
            return None
        return runtime

    def _mask_secret(self, value: str) -> str:
        secret = str(value or "").strip()
        if not secret:
            return ""
        if len(secret) <= 8:
            return f"{secret[:2]}***{secret[-2:]}"
        return f"{secret[:4]}***{secret[-4:]}"


__all__ = ["ModelConfigStore", "SYSTEM_SECURITY_CONSTRAINT"]
