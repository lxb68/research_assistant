"""统一适配云端模型供应商和 Ollama、LM Studio 等本地模型服务。"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlparse

import requests


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


def chat_completion(
    model: dict[str, str],
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    timeout: int = 60,
    response_format: dict[str, Any] | None = None,
) -> str:
    """按运行时协议发送聊天请求，并统一返回纯文本回答。"""
    protocol = normalize_protocol(model.get("protocol", ""), model.get("provider", "custom"))
    if protocol == "ollama":
        return _chat_ollama(
            model,
            messages,
            temperature=temperature,
            timeout=timeout,
            response_format=response_format,
        )
    if protocol == "anthropic":
        return _chat_anthropic(
            model,
            messages,
            temperature=temperature,
            timeout=timeout,
            response_format=response_format,
        )
    if protocol == "gemini":
        return _chat_gemini(
            model,
            messages,
            temperature=temperature,
            timeout=timeout,
            response_format=response_format,
        )
    return _chat_openai_compatible(
        model,
        messages,
        temperature=temperature,
        timeout=timeout,
        response_format=response_format,
    )


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


def _chat_openai_compatible(
    model: dict[str, str],
    messages: list[dict[str, str]],
    *,
    temperature: float,
    timeout: int,
    response_format: dict[str, Any] | None,
) -> str:
    """调用 OpenAI Chat Completions 兼容接口。"""
    request_body: dict[str, Any] = {
        "model": _require_model_name(model),
        "messages": messages,
        "temperature": temperature,
    }
    if response_format:
        request_body["response_format"] = response_format
        request_body["max_tokens"] = 4096
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
            if key not in {"response_format", "max_tokens"}
        }
        response = requests.post(
            _join_endpoint(_require_base_url(model), "chat/completions"),
            headers=_bearer_headers(model.get("api_key", "")),
            json=fallback_body,
            timeout=timeout,
        )
    payload = _response_json(response)
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        raise RuntimeError("模型没有返回有效回答")
    content = choices[0].get("message", {}).get("content") if isinstance(choices[0], dict) else ""
    return _require_answer(content)


def _chat_ollama(
    model: dict[str, str],
    messages: list[dict[str, str]],
    *,
    temperature: float,
    timeout: int,
    response_format: dict[str, Any] | None,
) -> str:
    """调用 Ollama 原生聊天接口，并关闭流式响应以统一上层处理。"""
    request_body: dict[str, Any] = {
        "model": _require_model_name(model),
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if response_format and response_format.get("type") == "json_object":
        request_body["format"] = "json"
    response = requests.post(
        _ollama_endpoint(_require_base_url(model), "chat"),
        json=request_body,
        timeout=timeout,
    )
    payload = _response_json(response)
    content = payload.get("message", {}).get("content") if isinstance(payload, dict) else ""
    return _require_answer(content)


def _chat_anthropic(
    model: dict[str, str],
    messages: list[dict[str, str]],
    *,
    temperature: float,
    timeout: int,
    response_format: dict[str, Any] | None,
) -> str:
    """把通用消息转换为 Anthropic Messages API 请求。"""
    system_messages = [item["content"] for item in messages if item.get("role") == "system" and item.get("content")]
    conversation = [
        {"role": item["role"], "content": item["content"]}
        for item in messages
        if item.get("role") in {"user", "assistant"} and item.get("content")
    ]
    response = requests.post(
        _join_endpoint(_require_base_url(model), "messages"),
        headers=_anthropic_headers(model.get("api_key", "")),
        json={
            "model": _require_model_name(model),
            "system": "\n\n".join(system_messages),
            "messages": conversation,
            "temperature": temperature,
            "max_tokens": 4096,
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
    return _require_answer(content)


def _chat_gemini(
    model: dict[str, str],
    messages: list[dict[str, str]],
    *,
    temperature: float,
    timeout: int,
    response_format: dict[str, Any] | None,
) -> str:
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
    return _require_answer(content)


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


def _require_answer(content: Any) -> str:
    """规范化模型回答，并拒绝空响应。"""
    answer = str(content or "").strip()
    if not answer:
        raise RuntimeError("模型返回了空回答")
    return answer


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
