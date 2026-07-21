"""腾讯云机器翻译客户端，集中维护 TC3 签名和响应校验。"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import time
from urllib.request import Request, urlopen


TENCENT_TMT_HOST = "tmt.tencentcloudapi.com"
TENCENT_TMT_ACTION = "TextTranslate"
TENCENT_TMT_VERSION = "2018-03-21"
TENCENT_TMT_SERVICE = "tmt"


def translate_tencent_cloud(
    text: str,
    *,
    secret_id: str,
    secret_key: str,
    region: str,
    timeout: int = 10,
) -> str:
    """调用腾讯云 TMT；失败时抛出可供 API 和业务层处理的异常。"""
    if not secret_id.strip() or not secret_key.strip():
        raise ValueError("未配置腾讯云 SecretId 或 SecretKey")

    timestamp = int(time.time())
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    payload = json.dumps(
        {
            "SourceText": text,
            "Source": "zh",
            "Target": "en",
            "ProjectId": 0,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    canonical_headers = (
        "content-type:application/json; charset=utf-8\n"
        f"host:{TENCENT_TMT_HOST}\n"
        f"x-tc-action:{TENCENT_TMT_ACTION.lower()}\n"
    )
    signed_headers = "content-type;host;x-tc-action"
    canonical_request = "\n".join(
        [
            "POST",
            "/",
            "",
            canonical_headers,
            signed_headers,
            hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        ],
    )
    credential_scope = f"{date}/{TENCENT_TMT_SERVICE}/tc3_request"
    string_to_sign = "\n".join(
        [
            "TC3-HMAC-SHA256",
            str(timestamp),
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ],
    )
    secret_date = hmac.new(
        f"TC3{secret_key}".encode("utf-8"),
        date.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    secret_service = hmac.new(secret_date, TENCENT_TMT_SERVICE.encode("utf-8"), hashlib.sha256).digest()
    secret_signing = hmac.new(secret_service, b"tc3_request", hashlib.sha256).digest()
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        "TC3-HMAC-SHA256 "
        f"Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    request = Request(
        f"https://{TENCENT_TMT_HOST}",
        data=payload.encode("utf-8"),
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json; charset=utf-8",
            "Host": TENCENT_TMT_HOST,
            "X-TC-Action": TENCENT_TMT_ACTION,
            "X-TC-Version": TENCENT_TMT_VERSION,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Region": region,
        },
    )

    with urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    response_data = data.get("Response", {})
    error_data = response_data.get("Error")
    if error_data:
        code = str(error_data.get("Code") or "未知错误")
        message = str(error_data.get("Message") or "腾讯云翻译请求失败")
        raise RuntimeError(f"{code}: {message}")
    translated = str(response_data.get("TargetText") or "").strip()
    if not translated:
        raise RuntimeError("腾讯云翻译返回了无法识别的响应")
    return translated


__all__ = ["translate_tencent_cloud"]
