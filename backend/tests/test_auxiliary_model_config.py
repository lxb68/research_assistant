"""验证辅助模型只服务轻量任务，并在配置不完整时回退主模型。"""

from __future__ import annotations

from unittest.mock import Mock, patch

from app.core.config import settings
from app.services.model_config import ModelConfigStore


def test_auxiliary_model_falls_back_to_primary_when_not_configured() -> None:
    store = ModelConfigStore.__new__(ModelConfigStore)
    store.build_model_payload = Mock(return_value={"model": "primary"})

    with (
        patch.object(settings, "auxiliary_model_name", ""),
        patch.object(settings, "auxiliary_model_base_url", ""),
    ):
        result = store.build_auxiliary_model_payload()

    assert result == {"model": "primary"}


def test_auxiliary_model_builds_independent_runtime_payload() -> None:
    store = ModelConfigStore.__new__(ModelConfigStore)

    with (
        patch.object(settings, "auxiliary_model_name", "fast-model"),
        patch.object(settings, "auxiliary_model_base_url", "http://127.0.0.1:11434"),
        patch.object(settings, "auxiliary_model_provider", "ollama"),
        patch.object(settings, "auxiliary_model_protocol", "ollama"),
        patch.object(settings, "auxiliary_model_api_key", ""),
    ):
        result = store.build_auxiliary_model_payload()

    assert result is not None
    assert result["model"] == "fast-model"
    assert result["provider"] == "ollama"
    assert result["protocol"] == "ollama"
