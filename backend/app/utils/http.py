"""封装第三方 HTTP GET 请求、查询参数和统一异常信息。"""

from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
import json

from app.core.config import settings


def get_json(url: str, params: dict | None = None, headers: dict | None = None) -> dict:
    """发送 GET 请求并解析 JSON 响应。"""
    full_url = build_url(url, params)
    request = Request(full_url, headers=headers or {})

    try:
        with urlopen(request, timeout=settings.request_timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(f"第三方接口返回 HTTP {error.code}: {error.reason}") from error
    except URLError as error:
        raise RuntimeError(f"无法连接第三方接口: {error.reason}") from error


def get_text(url: str, params: dict | None = None, headers: dict | None = None) -> str:
    """发送 GET 请求并返回文本内容，适合 XML/Atom 响应。"""
    full_url = build_url(url, params)
    request = Request(full_url, headers=headers or {})

    try:
        with urlopen(request, timeout=settings.request_timeout) as response:
            return response.read().decode("utf-8")
    except HTTPError as error:
        raise RuntimeError(f"第三方接口返回 HTTP {error.code}: {error.reason}") from error
    except URLError as error:
        raise RuntimeError(f"无法连接第三方接口: {error.reason}") from error


def build_url(url: str, params: dict | None = None) -> str:
    """拼接 URL 查询参数。"""
    if not params:
        return url

    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(params)}"
