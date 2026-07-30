"""统一适配云端模型供应商和 Ollama、LM Studio 等本地模型服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import requests

from app.core.config import settings
from app.services.model_call_limiter import ModelCallLimiter


_MODEL_CALL_LIMITER = ModelCallLimiter(settings.agent_model_max_concurrency)


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """统一表示不同模型协议返回的 Token 用量。"""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ModelCallResult:
    """保留模型正文、用量与上游请求标识，供业务层审计。"""

    content: str
    usage: ModelUsage
    request_id: str = ""
    finish_reason: str = ""


class ModelCallError(RuntimeError):
    """携带可重试性和 HTTP 语义的模型调用异常。"""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        http_status: int | None = None,
        retryable: bool = False,
        request_accepted: bool | None = None,
        request_id: str = "",
        finish_reason: str = "",
        usage: ModelUsage | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.http_status = http_status
        self.retryable = retryable
        self.request_accepted = request_accepted
        self.request_id = request_id
        self.finish_reason = finish_reason
        self.usage = usage or ModelUsage()


# 供应商目录只保存稳定的协议与默认地址；模型列表优先通过服务端实时发现。
MODEL_PROVIDERS: tuple[dict[str, Any], ...] = (
    {
        "id": "openai",
        "name": "OpenAI",
        "protocol": "openai_compatible",
        "baseUrl": "https://api.openai.com/v1",
        "requiresApiKey": True,
        "modelPlaceholder": "例如 gpt-4o-mini",
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "protocol": "openai_compatible",
        "baseUrl": "https://api.deepseek.com",
        "requiresApiKey": True,
        "modelPlaceholder": "例如 deepseek-chat",
    },
    {
        "id": "qwen",
        "name": "阿里云百炼（通义千问）",
        "protocol": "openai_compatible",
        "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "requiresApiKey": True,
        "modelPlaceholder": "例如 qwen-plus",
    },
    {
        "id": "moonshot",
        "name": "Moonshot / Kimi",
        "protocol": "openai_compatible",
        "baseUrl": "https://api.moonshot.cn/v1",
        "requiresApiKey": True,
        "modelPlaceholder": "例如 kimi-k2.5",
    },
    {
        "id": "zhipu",
        "name": "智谱 AI",
        "protocol": "openai_compatible",
        "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
        "requiresApiKey": True,
        "modelPlaceholder": "例如 glm-4.5",
    },
    {
        "id": "siliconflow",
        "name": "硅基流动",
        "protocol": "openai_compatible",
        "baseUrl": "https://api.siliconflow.cn/v1",
        "requiresApiKey": True,
        "modelPlaceholder": "请输入平台提供的模型 ID",
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "protocol": "openai_compatible",
        "baseUrl": "https://openrouter.ai/api/v1",
        "requiresApiKey": True,
        "modelPlaceholder": "例如 openai/gpt-4o-mini",
    },
    {
        "id": "anthropic",
        "name": "Anthropic Claude",
        "protocol": "anthropic",
        "baseUrl": "https://api.anthropic.com/v1",
        "requiresApiKey": True,
        "modelPlaceholder": "请输入 Claude 模型 ID",
    },
    {
        "id": "gemini",
        "name": "Google Gemini",
        "protocol": "gemini",
        "baseUrl": "https://generativelanguage.googleapis.com/v1beta",
        "requiresApiKey": True,
        "modelPlaceholder": "例如 gemini-2.5-flash",
    },
    {
        "id": "ollama",
        "name": "Ollama（本地）",
        "protocol": "ollama",
        "baseUrl": "http://127.0.0.1:11434",
        "requiresApiKey": False,
        "modelPlaceholder": "例如 qwen3:8b",
    },
    {
        "id": "lmstudio",
        "name": "LM Studio（本地）",
        "protocol": "openai_compatible",
        "baseUrl": "http://127.0.0.1:1234/v1",
        "requiresApiKey": False,
        "modelPlaceholder": "启动本地服务后点击“发现模型”",
    },
    {
        "id": "custom",
        "name": "自定义服务",
        "protocol": "openai_compatible",
        "baseUrl": "",
        "requiresApiKey": False,
        "modelPlaceholder": "请输入服务端使用的模型 ID",
    },
)

SUPPORTED_PROTOCOLS = {"openai_compatible", "ollama", "anthropic", "gemini"}


def get_provider(provider_id: str) -> dict[str, Any]:
    """按 ID 获取供应商配置，未知值回退到自定义服务。"""
    normalized = str(provider_id or "").strip().lower()
    return next((dict(item) for item in MODEL_PROVIDERS if item["id"] == normalized), dict(MODEL_PROVIDERS[-1]))


def infer_provider(base_url: str) -> str:
    """根据旧配置的 Base URL 推断供应商，保证历史配置可继续使用。"""
    normalized = str(base_url or "").strip().lower()
    host_markers = {
        "api.openai.com": "openai",
        "api.deepseek.com": "deepseek",
        "dashscope.aliyuncs.com": "qwen",
        "api.moonshot.cn": "moonshot",
        "open.bigmodel.cn": "zhipu",
        "api.siliconflow.cn": "siliconflow",
        "openrouter.ai": "openrouter",
        "api.anthropic.com": "anthropic",
        "generativelanguage.googleapis.com": "gemini",
        ":11434": "ollama",
        ":1234": "lmstudio",
    }
    return next((provider for marker, provider in host_markers.items() if marker in normalized), "custom")


def normalize_protocol(protocol: str, provider: str) -> str:
    """校验协议名称，并在缺失时使用供应商默认协议。"""
    normalized = str(protocol or "").strip().lower()
    if normalized in SUPPORTED_PROTOCOLS:
        return normalized
    return str(get_provider(provider)["protocol"])


def requires_api_key(provider: str, protocol: str) -> bool:
    """判断当前供应商与协议是否必须提供 API Key。"""
    provider_config = get_provider(provider)
    if provider_config["id"] != "custom":
        return bool(provider_config["requiresApiKey"])
    return protocol in {"anthropic", "gemini"}


def validate_base_url(base_url: str) -> str:
    """校验模型服务地址并返回去除末尾斜杠后的规范值。"""
    value = str(base_url or "").strip().rstrip("/")
    if not value:
        raise ValueError("请先填写模型 Base URL")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("模型 Base URL 必须是有效的 http 或 https 地址")
    return value


def chat_completion_result(
    model: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    timeout: int = 60,
    response_format: dict[str, Any] | None = None,
    max_output_tokens: int | None = None,
    thinking: bool | None = None,
) -> ModelCallResult:
    """按运行时协议发送聊天请求，并保留正文、用量和请求标识。"""
    with _MODEL_CALL_LIMITER.slot():
        protocol = normalize_protocol(
            model.get("protocol", ""),
            model.get("provider", "custom"),
        )
        if protocol == "ollama":
            return _chat_ollama_result(
                model,
                messages,
                temperature=temperature,
                timeout=timeout,
                response_format=response_format,
                max_output_tokens=max_output_tokens,
            )
        if protocol == "anthropic":
            return _chat_anthropic_result(
                model,
                messages,
                temperature=temperature,
                timeout=timeout,
                response_format=response_format,
                max_output_tokens=max_output_tokens,
            )
        if protocol == "gemini":
            return _chat_gemini_result(
                model,
                messages,
                temperature=temperature,
                timeout=timeout,
                response_format=response_format,
                max_output_tokens=max_output_tokens,
            )
        return _chat_openai_compatible_result(
            model,
            messages,
            temperature=temperature,
            timeout=timeout,
            response_format=response_format,
            max_output_tokens=max_output_tokens,
            thinking=thinking,
        )


def chat_completion(
    model: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    timeout: int = 60,
    response_format: dict[str, Any] | None = None,
    max_output_tokens: int | None = None,
    thinking: bool | None = None,
) -> str:
    """保持原有纯文本接口兼容，详细计量请使用 chat_completion_result。"""
    return chat_completion_result(
        model,
        messages,
        temperature=temperature,
        timeout=timeout,
        response_format=response_format,
        max_output_tokens=max_output_tokens,
        thinking=thinking,
    ).content


def discover_models(model: dict[str, str], *, timeout: int = 15) -> list[str]:
    """调用供应商模型列表接口，返回去重并排序后的模型 ID。"""
    protocol = normalize_protocol(model.get("protocol", ""), model.get("provider", "custom"))
    base_url = _require_base_url(model)
    api_key = str(model.get("api_key") or "").strip()

    if protocol == "ollama":
        response = requests.get(_ollama_endpoint(base_url, "tags"), timeout=timeout)
        payload = _response_json(response)
        models = payload.get("models") if isinstance(payload, dict) else []
        values = [str(item.get("name") or item.get("model") or "") for item in models or [] if isinstance(item, dict)]
    elif protocol == "anthropic":
        response = requests.get(
            _join_endpoint(base_url, "models"),
            headers=_anthropic_headers(api_key),
            timeout=timeout,
        )
        payload = _response_json(response)
        values = [str(item.get("id") or "") for item in payload.get("data", []) if isinstance(item, dict)]
    elif protocol == "gemini":
        response = requests.get(
            _join_endpoint(base_url, "models"),
            headers={"x-goog-api-key": api_key},
            timeout=timeout,
        )
        payload = _response_json(response)
        values = [
            str(item.get("name") or "").removeprefix("models/")
            for item in payload.get("models", [])
            if isinstance(item, dict) and "generateContent" in (item.get("supportedGenerationMethods") or [])
        ]
    else:
        response = requests.get(
            _join_endpoint(base_url, "models"),
            headers=_bearer_headers(api_key),
            timeout=timeout,
        )
        payload = _response_json(response)
        values = [str(item.get("id") or item.get("name") or "") for item in payload.get("data", []) if isinstance(item, dict)]

    return sorted({value.strip() for value in values if value and value.strip()}, key=str.lower)


def _chat_openai_compatible_result(
    model: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    temperature: float,
    timeout: int,
    response_format: dict[str, Any] | None,
    max_output_tokens: int | None,
    thinking: bool | None,
) -> ModelCallResult:
    """调用 OpenAI Chat Completions 兼容接口。"""
    request_body: dict[str, Any] = {
        "model": _require_model_name(model),
        "messages": messages,
        "temperature": temperature,
    }
    if response_format:
        request_body["response_format"] = response_format
    provider = str(model.get("provider") or infer_provider(model.get("base_url", ""))).strip().lower()
    if thinking is not None and provider == "deepseek":
        request_body["thinking"] = {"type": "enabled" if thinking else "disabled"}
    output_limit = _resolve_max_output_tokens(model, max_output_tokens)
    if output_limit is not None:
        request_body["max_tokens"] = output_limit
    response = requests.post(
        _join_endpoint(_require_base_url(model), "chat/completions"),
        headers=_bearer_headers(model.get("api_key", "")),
        json=request_body,
        timeout=timeout,
    )
    if response_format and _response_format_is_unsupported(response):
        fallback_body = {
            key: value
            for key, value in request_body.items()
            if key != "response_format"
        }
        response = requests.post(
            _join_endpoint(_require_base_url(model), "chat/completions"),
            headers=_bearer_headers(model.get("api_key", "")),
            json=fallback_body,
            timeout=timeout,
        )
    payload = _response_json(response)
    choices = payload.get("choices") if isinstance(payload, dict) else None
    usage = _openai_usage(payload)
    request_id = _request_id(response)
    if not choices:
        _require_answer(
            None,
            usage=usage,
            request_id=request_id,
            max_output_tokens=output_limit,
        )
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    finish_reason = _finish_reason(choice.get("finish_reason"))
    return ModelCallResult(
        content=_require_answer(
            message.get("content"),
            usage=usage,
            request_id=request_id,
            finish_reason=finish_reason,
            max_output_tokens=output_limit,
        ),
        usage=usage,
        request_id=request_id,
        finish_reason=finish_reason,
    )


def _chat_ollama_result(
    model: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    temperature: float,
    timeout: int,
    response_format: dict[str, Any] | None,
    max_output_tokens: int | None,
) -> ModelCallResult:
    """调用 Ollama 原生聊天接口，并关闭流式响应以统一上层处理。"""
    request_body: dict[str, Any] = {
        "model": _require_model_name(model),
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if response_format and response_format.get("type") == "json_object":
        request_body["format"] = "json"
    output_limit = _resolve_max_output_tokens(model, max_output_tokens)
    if output_limit is not None:
        request_body["options"]["num_predict"] = output_limit
    response = requests.post(
        _ollama_endpoint(_require_base_url(model), "chat"),
        json=request_body,
        timeout=timeout,
    )
    payload = _response_json(response)
    content = payload.get("message", {}).get("content") if isinstance(payload, dict) else ""
    prompt_tokens = _optional_int(payload.get("prompt_eval_count"))
    completion_tokens = _optional_int(payload.get("eval_count"))
    usage = ModelUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=_sum_optional(prompt_tokens, completion_tokens),
    )
    request_id = _request_id(response)
    finish_reason = _finish_reason(payload.get("done_reason"))
    return ModelCallResult(
        content=_require_answer(
            content,
            usage=usage,
            request_id=request_id,
            finish_reason=finish_reason,
            max_output_tokens=output_limit,
        ),
        usage=usage,
        request_id=request_id,
        finish_reason=finish_reason,
    )


def _chat_anthropic_result(
    model: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    temperature: float,
    timeout: int,
    response_format: dict[str, Any] | None,
    max_output_tokens: int | None,
) -> ModelCallResult:
    """把通用消息转换为 Anthropic Messages API 请求。"""
    system_messages = [item["content"] for item in messages if item.get("role") == "system" and item.get("content")]
    conversation = [
        {"role": item["role"], "content": item["content"]}
        for item in messages
        if item.get("role") in {"user", "assistant"} and item.get("content")
    ]
    output_limit = _resolve_max_output_tokens(model, max_output_tokens)
    if output_limit is None:
        raise ModelCallError(
            "Anthropic 协议需要配置 max_output_tokens",
            category="invalid_configuration",
        )
    response = requests.post(
        _join_endpoint(_require_base_url(model), "messages"),
        headers=_anthropic_headers(model.get("api_key", "")),
        json={
            "model": _require_model_name(model),
            "system": "\n\n".join(system_messages),
            "messages": conversation,
            "temperature": temperature,
            "max_tokens": output_limit,
        },
        timeout=timeout,
    )
    payload = _response_json(response)
    parts = payload.get("content") if isinstance(payload, dict) else []
    content = "\n".join(
        str(item.get("text") or "")
        for item in parts or []
        if isinstance(item, dict) and item.get("type") == "text"
    )
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    prompt_tokens = _optional_int(usage.get("input_tokens"))
    completion_tokens = _optional_int(usage.get("output_tokens"))
    model_usage = ModelUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=_sum_optional(prompt_tokens, completion_tokens),
        cached_tokens=_optional_int(usage.get("cache_read_input_tokens")),
    )
    request_id = _request_id(response)
    finish_reason = _finish_reason(payload.get("stop_reason"))
    return ModelCallResult(
        content=_require_answer(
            content,
            usage=model_usage,
            request_id=request_id,
            finish_reason=finish_reason,
            max_output_tokens=output_limit,
        ),
        usage=model_usage,
        request_id=request_id,
        finish_reason=finish_reason,
    )


def _chat_gemini_result(
    model: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    temperature: float,
    timeout: int,
    response_format: dict[str, Any] | None,
    max_output_tokens: int | None,
) -> ModelCallResult:
    """把通用消息转换为 Gemini generateContent 请求。"""
    system_messages = [item["content"] for item in messages if item.get("role") == "system" and item.get("content")]
    contents = [
        {
            "role": "model" if item.get("role") == "assistant" else "user",
            "parts": [{"text": item["content"]}],
        }
        for item in messages
        if item.get("role") in {"user", "assistant"} and item.get("content")
    ]
    model_name = _require_model_name(model).removeprefix("models/")
    generation_config: dict[str, Any] = {"temperature": temperature}
    if response_format and response_format.get("type") == "json_object":
        generation_config["responseMimeType"] = "application/json"
    output_limit = _resolve_max_output_tokens(model, max_output_tokens)
    if output_limit is not None:
        generation_config["maxOutputTokens"] = output_limit
    response = requests.post(
        f"{_require_base_url(model)}/models/{quote(model_name, safe='')}:generateContent",
        headers={"Content-Type": "application/json", "x-goog-api-key": str(model.get("api_key") or "")},
        json={
            "systemInstruction": {"parts": [{"text": "\n\n".join(system_messages)}]},
            "contents": contents,
            "generationConfig": generation_config,
        },
        timeout=timeout,
    )
    payload = _response_json(response)
    candidates = payload.get("candidates") if isinstance(payload, dict) else []
    parts = candidates[0].get("content", {}).get("parts", []) if candidates and isinstance(candidates[0], dict) else []
    content = "\n".join(str(item.get("text") or "") for item in parts if isinstance(item, dict))
    usage = payload.get("usageMetadata") if isinstance(payload.get("usageMetadata"), dict) else {}
    model_usage = ModelUsage(
        prompt_tokens=_optional_int(usage.get("promptTokenCount")),
        completion_tokens=_optional_int(usage.get("candidatesTokenCount")),
        total_tokens=_optional_int(usage.get("totalTokenCount")),
        cached_tokens=_optional_int(usage.get("cachedContentTokenCount")),
        reasoning_tokens=_optional_int(usage.get("thoughtsTokenCount")),
    )
    request_id = _request_id(response)
    finish_reason = _finish_reason(
        candidates[0].get("finishReason")
        if candidates and isinstance(candidates[0], dict)
        else "",
    )
    return ModelCallResult(
        content=_require_answer(
            content,
            usage=model_usage,
            request_id=request_id,
            finish_reason=finish_reason,
            max_output_tokens=output_limit,
        ),
        usage=model_usage,
        request_id=request_id,
        finish_reason=finish_reason,
    )


def _response_format_is_unsupported(response: requests.Response) -> bool:
    """仅在上游明确拒绝 response_format 时降级为普通文本调用。"""
    if int(getattr(response, "status_code", 0) or 0) not in {400, 404, 422}:
        return False
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return False
    detail = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        message = str(detail.get("message") or detail.get("type") or "")
    else:
        message = str(detail or "")
    lowered = message.lower()
    return any(token in lowered for token in ("response_format", "json_object", "json mode", "unsupported format"))


def _response_json(response: requests.Response) -> dict[str, Any]:
    """统一检查 HTTP 状态并解析 JSON，避免在错误中暴露请求密钥。"""
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        message = ""
        try:
            error_payload = response.json()
            detail = error_payload.get("error") if isinstance(error_payload, dict) else None
            if isinstance(detail, dict):
                message = str(detail.get("message") or detail.get("type") or "")
            elif detail:
                message = str(detail)
        except (ValueError, TypeError):
            message = ""
        suffix = f": {message[:300]}" if message else ""
        raise ModelCallError(
            f"模型服务返回 HTTP {status}{suffix}",
            category=_http_error_category(status),
            http_status=status,
            retryable=status == 429 or 500 <= status <= 599,
            request_accepted=False,
            request_id=_request_id(response),
        )
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        message = ""
        try:
            payload = response.json()
            detail = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(detail, dict):
                message = str(detail.get("message") or detail.get("type") or "")
            elif detail:
                message = str(detail)
        except (ValueError, TypeError):
            message = ""
        suffix = f"：{message[:300]}" if message else ""
        raise RuntimeError(f"模型服务返回 HTTP {response.status_code}{suffix}") from error
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError("模型服务返回了无效 JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError("模型服务返回的数据结构无效")
    return payload


def _openai_usage(payload: dict[str, Any]) -> ModelUsage:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    prompt_details = (
        usage.get("prompt_tokens_details")
        if isinstance(usage.get("prompt_tokens_details"), dict)
        else {}
    )
    completion_details = (
        usage.get("completion_tokens_details")
        if isinstance(usage.get("completion_tokens_details"), dict)
        else {}
    )
    return ModelUsage(
        prompt_tokens=_optional_int(usage.get("prompt_tokens")),
        completion_tokens=_optional_int(usage.get("completion_tokens")),
        total_tokens=_optional_int(usage.get("total_tokens")),
        cached_tokens=_optional_int(prompt_details.get("cached_tokens")),
        reasoning_tokens=_optional_int(completion_details.get("reasoning_tokens")),
    )


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _sum_optional(*values: Any) -> int | None:
    available = [parsed for value in values if (parsed := _optional_int(value)) is not None]
    return sum(available) if available else None


def _resolve_max_output_tokens(
    model: dict[str, Any],
    explicit: int | None,
) -> int | None:
    """优先使用调用场景预算，其次使用运行时配置；未配置时不注入协议参数。"""
    value = explicit if explicit is not None else model.get("max_output_tokens")
    parsed = _optional_int(value)
    return parsed if parsed and parsed > 0 else None


def _finish_reason(value: Any) -> str:
    """统一不同供应商的停止原因，供业务层识别长度截断。"""
    return str(value or "").strip().lower()


def _request_id(response: requests.Response) -> str:
    headers = getattr(response, "headers", {}) or {}
    return str(
        headers.get("x-request-id")
        or headers.get("request-id")
        or headers.get("x-ratelimit-request-id")
        or ""
    )


def _http_error_category(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if status in {401, 403}:
        return "authentication"
    if status == 402:
        return "quota_exhausted"
    if status == 404:
        return "not_found"
    if 500 <= status <= 599:
        return "upstream"
    return "request_rejected"


def _join_endpoint(base_url: str, endpoint: str) -> str:
    """在不重复路径的情况下拼接 API 根地址和端点。"""
    base = str(base_url or "").strip().rstrip("/")
    suffix = str(endpoint or "").strip().strip("/")
    if base.lower().endswith(f"/{suffix.lower()}"):
        return base
    return f"{base}/{suffix}"


def _ollama_endpoint(base_url: str, endpoint: str) -> str:
    """兼容用户填写 Ollama 根地址或以 /api 结尾的地址。"""
    base = str(base_url or "").strip().rstrip("/")
    suffix = str(endpoint or "").strip().strip("/")
    return f"{base}/{suffix}" if base.lower().endswith("/api") else f"{base}/api/{suffix}"


def _bearer_headers(api_key: str) -> dict[str, str]:
    """构造可选 Bearer 认证头，本地兼容服务允许无密钥。"""
    headers = {"Content-Type": "application/json"}
    normalized = str(api_key or "").strip()
    if normalized:
        headers["Authorization"] = f"Bearer {normalized}"
    return headers


def _anthropic_headers(api_key: str) -> dict[str, str]:
    """构造 Anthropic API 所需的认证与版本请求头。"""
    return {
        "Content-Type": "application/json",
        "x-api-key": str(api_key or "").strip(),
        "anthropic-version": "2023-06-01",
    }


def _require_base_url(model: dict[str, str]) -> str:
    """读取必需的 Base URL，并移除末尾斜杠。"""
    return validate_base_url(str(model.get("base_url") or ""))


def _require_model_name(model: dict[str, str]) -> str:
    """读取必需的模型名称。"""
    value = str(model.get("model") or "").strip()
    if not value:
        raise ValueError("请先填写模型名称")
    return value


def _require_answer(
    content: Any,
    *,
    usage: ModelUsage | None = None,
    request_id: str = "",
    finish_reason: str = "",
    max_output_tokens: int | None = None,
) -> str:
    """规范化模型回答，并把空响应转换为可诊断、可配置重试的错误。"""
    answer = str(content or "").strip()
    if answer:
        return answer

    normalized_reason = _finish_reason(finish_reason)
    if normalized_reason in {"length", "max_tokens"}:
        category = "output_truncated"
        retryable = False
    elif normalized_reason in {"content_filter", "safety"}:
        category = "content_filtered"
        retryable = False
    elif normalized_reason in {"insufficient_system_resource", "overloaded"}:
        category = "upstream"
        retryable = True
    else:
        category = "empty_response"
        retryable = True

    model_usage = usage or ModelUsage()
    diagnostics: list[str] = []
    if normalized_reason:
        diagnostics.append(f"finish_reason={normalized_reason}")
    if model_usage.completion_tokens is not None:
        diagnostics.append(f"completion_tokens={model_usage.completion_tokens}")
    if model_usage.reasoning_tokens is not None:
        diagnostics.append(f"reasoning_tokens={model_usage.reasoning_tokens}")
    if max_output_tokens is not None:
        diagnostics.append(f"max_output_tokens={max_output_tokens}")
    suffix = f"（{', '.join(diagnostics)}）" if diagnostics else ""
    if category == "output_truncated":
        reasoning_exhausted_budget = (
            model_usage.reasoning_tokens is not None
            and model_usage.completion_tokens is not None
            and model_usage.completion_tokens > 0
            and model_usage.reasoning_tokens >= model_usage.completion_tokens
        )
        if reasoning_exhausted_budget:
            message = (
                f"模型推理过程耗尽输出预算且未生成最终回答{suffix}，"
                "请关闭该调用场景的思考模式，或提高输出 Token 配置"
            )
        else:
            message = f"模型输出预算耗尽且未生成最终回答{suffix}，请提高调用场景的输出 Token 配置"
    elif category == "content_filtered":
        message = f"模型回答被内容过滤器清空{suffix}"
    elif category == "upstream":
        message = f"模型服务资源不足，未生成最终回答{suffix}"
    else:
        message = f"模型返回了空回答{suffix}"
    raise ModelCallError(
        message,
        category=category,
        retryable=retryable,
        request_accepted=True,
        request_id=request_id,
        finish_reason=normalized_reason,
        usage=model_usage,
    )


__all__ = [
    "MODEL_PROVIDERS",
    "SUPPORTED_PROTOCOLS",
    "chat_completion",
    "discover_models",
    "get_provider",
    "infer_provider",
    "normalize_protocol",
    "requires_api_key",
    "validate_base_url",
]
