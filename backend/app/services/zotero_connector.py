"""Zotero 本地只读 API 连接器。"""

from __future__ import annotations

import ipaddress
from pathlib import Path
import re
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import requests


class ZoteroConnectionError(RuntimeError):
    """Zotero 本地 API 不可访问或返回了无效数据。"""


class ZoteroConnector:
    """封装 Zotero Local API，只允许访问本机回环地址。"""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:23119/api",
        library_type: str = "users",
        library_id: str = "0",
        timeout: int = 15,
    ) -> None:
        self.base_url = self._validate_base_url(base_url)
        normalized_type = str(library_type or "users").strip().lower()
        if normalized_type not in {"users", "groups"}:
            raise ValueError("Zotero library_type 只能是 users 或 groups")
        normalized_id = str(library_id or "0").strip()
        if not normalized_id.isdigit():
            raise ValueError("Zotero library_id 必须是数字")
        self.library_type = normalized_type
        self.library_id = normalized_id
        self.timeout = max(1, int(timeout))

    @property
    def library_prefix(self) -> str:
        return f"/{self.library_type}/{self.library_id}"

    def test_connection(self) -> dict[str, Any]:
        response = self._request("/")
        return {
            "status": "ok",
            "apiVersion": response.headers.get("Zotero-API-Version", "3"),
            "schemaVersion": response.headers.get("Zotero-Schema-Version", ""),
            "baseUrl": self.base_url,
        }

    def list_collections(self) -> list[dict[str, Any]]:
        values = self._get_json(f"{self.library_prefix}/collections")
        if not isinstance(values, list):
            raise ZoteroConnectionError("Zotero collections 返回格式无效")
        result: list[dict[str, Any]] = []
        for raw in values:
            if not isinstance(raw, dict):
                continue
            data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
            key = str(data.get("key") or raw.get("key") or "").strip()
            if not key:
                continue
            result.append({
                "key": key,
                "name": str(data.get("name") or key),
                "parentCollection": (
                    str(data.get("parentCollection") or "")
                    if data.get("parentCollection") is not False
                    else ""
                ),
                "version": int(data.get("version") or raw.get("version") or 0),
            })
        return result

    def list_top_items(self, collection_key: str | None = None) -> list[dict[str, Any]]:
        if collection_key:
            key = self._validate_key(collection_key)
            path = f"{self.library_prefix}/collections/{key}/items/top"
        else:
            path = f"{self.library_prefix}/items/top"
        values = self._get_json(path)
        if not isinstance(values, list):
            raise ZoteroConnectionError("Zotero items 返回格式无效")
        return [item for item in values if isinstance(item, dict)]

    def list_children(self, parent_key: str) -> list[dict[str, Any]]:
        values = self._get_json(
            f"{self.library_prefix}/items/{self._validate_key(parent_key)}/children",
        )
        if not isinstance(values, list):
            raise ZoteroConnectionError("Zotero children 返回格式无效")
        return [item for item in values if isinstance(item, dict)]

    def resolve_attachment_path(self, attachment_key: str) -> Path | None:
        response = self._request(
            f"{self.library_prefix}/items/{self._validate_key(attachment_key)}/file/view/url",
        )
        raw_url = response.text.strip().strip('"')
        if not raw_url:
            return None
        parsed = urlparse(raw_url)
        if parsed.scheme.lower() != "file":
            raise ZoteroConnectionError("Zotero 附件接口没有返回本地 file URL")
        decoded = url2pathname(unquote(parsed.path))
        if parsed.netloc and parsed.netloc.lower() not in {"", "localhost"}:
            decoded = f"//{parsed.netloc}{decoded}"
        if re.match(r"^/[A-Za-z]:/", decoded):
            decoded = decoded[1:]
        path = Path(decoded).resolve()
        return path if path.is_file() else None

    def _get_json(self, path: str) -> Any:
        response = self._request(path)
        try:
            return response.json()
        except ValueError as error:
            raise ZoteroConnectionError("Zotero Local API 返回了无效 JSON") from error

    def _request(self, path: str) -> requests.Response:
        try:
            response = requests.get(
                f"{self.base_url}{path}",
                headers={"Zotero-API-Version": "3", "Accept": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as error:
            raise ZoteroConnectionError(
                "无法连接 Zotero，请确认 Zotero 已启动并已开启本地应用通信",
            ) from error
        if response.status_code == 403:
            raise ZoteroConnectionError("Zotero 拒绝访问，请在高级设置中开启本地应用通信")
        if not 200 <= response.status_code < 300:
            excerpt = (response.text or "").strip()[:300]
            raise ZoteroConnectionError(
                f"Zotero Local API 请求失败：HTTP {response.status_code} {excerpt}",
            )
        return response

    @staticmethod
    def _validate_base_url(value: str) -> str:
        normalized = str(value or "").strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme.lower() != "http" or not parsed.hostname:
            raise ValueError("Zotero Local API 必须使用本机 HTTP 地址")
        hostname = parsed.hostname.lower()
        is_loopback = hostname == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            raise ValueError("Zotero Local API 只允许连接本机回环地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Zotero Local API 地址格式无效")
        return normalized

    @staticmethod
    def _validate_key(value: str) -> str:
        normalized = str(value or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{8}", normalized):
            raise ValueError("Zotero Item Key 格式无效")
        return normalized


__all__ = ["ZoteroConnectionError", "ZoteroConnector"]
