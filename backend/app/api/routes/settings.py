"""Model provider discovery and persisted model settings routes."""

from fastapi import APIRouter, HTTPException

from app.schemas.api import ModelConfigRequest, ModelDiscoveryRequest
from app.services.model_client import discover_models
from app.services.model_config import ModelConfigStore


router = APIRouter()


@router.get("/api/settings/model-config")
def get_model_config() -> dict:
    return {"status": "ok", **ModelConfigStore().get_public_config()}


@router.get("/api/settings/model-providers")
def get_model_providers() -> dict:
    return {"status": "ok", "providers": ModelConfigStore().get_provider_catalog()}


@router.post("/api/settings/model-config/discover")
def discover_provider_models(payload: ModelDiscoveryRequest) -> dict:
    try:
        store = ModelConfigStore()
        candidate = store.build_candidate(
            provider=payload.provider,
            protocol=payload.protocol,
            base_url=payload.base_url,
            api_key=payload.api_key,
            allow_heuristic_fallback=payload.allow_heuristic_fallback,
        )
        models = discover_models(candidate)
        return {"status": "ok", "models": models, "count": len(models)}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"模型服务连接失败：{error}") from error


@router.post("/api/settings/model-config")
def save_model_config(payload: ModelConfigRequest) -> dict:
    try:
        result = ModelConfigStore().save(
            provider=payload.provider,
            protocol=payload.protocol,
            model=payload.model,
            base_url=payload.base_url,
            api_key=payload.api_key,
        )
        return {"status": "ok", **result}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


__all__ = ["router"]
