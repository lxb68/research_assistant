"""模型供应商发现、连接验证和持久化设置接口。"""

from time import perf_counter

from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.schemas.api import EnvConfigUpdateRequest, ExternalServiceConnectionTestRequest, ModelConfigRequest, ModelConnectionTestRequest, ModelDiscoveryRequest
from app.services.env_config import EnvConfigStore
from app.services.mineru import test_mineru_connection
from app.services.model_client import chat_completion, discover_models
from app.services.model_config import ModelConfigStore
from app.services.runtime_config import get_public_runtime_config
from app.services.tencent_translation import translate_tencent_cloud


router = APIRouter()


@router.get("/api/settings/model-config")
def get_model_config() -> dict:
    return {"status": "ok", **ModelConfigStore().get_public_config()}


@router.get("/api/settings/model-providers")
def get_model_providers() -> dict:
    return {"status": "ok", "providers": ModelConfigStore().get_provider_catalog()}


@router.get("/api/settings/runtime-config")
def get_runtime_config() -> dict:
    """返回脱敏后的当前运行参数与外部服务配置状态。"""
    return {"status": "ok", **get_public_runtime_config()}


@router.get("/api/settings/env-config")
def get_env_config() -> dict:
    """返回可编辑字段及脱敏状态，密钥字段永远不返回原文。"""
    return {"status": "ok", **EnvConfigStore().get_public_config()}


@router.post("/api/settings/env-config")
def update_env_config(payload: EnvConfigUpdateRequest) -> dict:
    """校验并原子写入 backend/.env；配置在后端重启后生效。"""
    try:
        return {"status": "ok", **EnvConfigStore().update(payload.values)}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"写入 .env 失败：{error}") from error


@router.post("/api/settings/external-service/test")
def test_external_service_connection(payload: ExternalServiceConnectionTestRequest) -> dict:
    """使用表单覆盖值或当前运行配置执行最小连通性测试，不持久化凭据。"""
    started = perf_counter()
    try:
        if payload.service == "tencent_translation":
            translated = translate_tencent_cloud(
                "连接测试",
                secret_id=payload.secret_id.strip() or settings.tencent_translation_secret_id,
                secret_key=payload.secret_key.strip() or settings.tencent_translation_secret_key,
                region=payload.region.strip() or settings.tencent_translation_region,
                timeout=min(settings.request_timeout, 20),
            )
            if not translated:
                raise RuntimeError("腾讯云翻译返回了空响应")
        else:
            test_mineru_connection(
                token=payload.token.strip() or settings.mineru_api_token,
                api_base=payload.api_base.strip() or settings.mineru_api_base,
                timeout=min(settings.mineru_request_timeout_seconds, 20),
            )
        return {
            "status": "ok",
            "available": True,
            "service": payload.service,
            "latencyMs": round((perf_counter() - started) * 1000),
        }
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        service_name = "腾讯云翻译" if payload.service == "tencent_translation" else "MinerU"
        raise HTTPException(status_code=502, detail=f"{service_name}连接测试失败：{error}") from error


@router.post("/api/settings/model-config/discover")
def discover_provider_models(payload: ModelDiscoveryRequest) -> dict:
    try:
        store = ModelConfigStore()
        candidate = store.build_candidate(
            provider=payload.provider,
            protocol=payload.protocol,
            base_url=payload.base_url,
            api_key=payload.api_key,
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
            allow_heuristic_fallback=payload.allow_heuristic_fallback,
        )
        return {"status": "ok", **result}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@router.post("/api/settings/model-config/test")
def test_model_connection(payload: ModelConnectionTestRequest) -> dict:
    """使用当前表单参数执行最小聊天请求，不持久化未保存配置。"""
    try:
        candidate = ModelConfigStore().build_candidate(
            provider=payload.provider,
            protocol=payload.protocol,
            base_url=payload.base_url,
            api_key=payload.api_key,
            model=payload.model,
        )
        started = perf_counter()
        answer = chat_completion(
            candidate,
            [
                {"role": "system", "content": "只回复 OK。不要输出任何密钥或配置。"},
                {"role": "user", "content": "连接测试"},
            ],
            temperature=0,
            timeout=20,
        )
        latency_ms = round((perf_counter() - started) * 1000)
        if not str(answer or "").strip():
            raise ValueError("模型返回了空响应")
        return {"status": "ok", "available": True, "latencyMs": latency_ms}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"模型连接测试失败：{error}") from error


@router.delete("/api/settings/model-config")
def clear_model_config() -> dict:
    """清除后端保存的模型配置；环境变量提供的配置不受影响。"""
    try:
        return {"status": "ok", **ModelConfigStore().clear()}
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


__all__ = ["router"]
