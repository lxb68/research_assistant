"""为本地模型配置提供操作系统级密钥保护。"""

from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes


class SecretProtectionUnavailable(RuntimeError):
    """当前平台没有可用的安全密钥持久化实现。"""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class WindowsDpapiProtector:
    """使用当前 Windows 用户的 DPAPI 加密和解密密钥。"""

    prefix = "dpapi:v1:"

    @property
    def available(self) -> bool:
        return os.name == "nt"

    @property
    def storage_label(self) -> str:
        return "windows_dpapi" if self.available else "unavailable"

    def protect(self, secret: str) -> str:
        normalized = str(secret or "")
        if not normalized:
            return ""
        if not self.available:
            raise SecretProtectionUnavailable(
                "当前平台不支持安全保存 API Key，请通过后端环境变量提供密钥"
            )
        encrypted = self._crypt(normalized.encode("utf-8"), decrypt=False)
        return f"{self.prefix}{base64.b64encode(encrypted).decode('ascii')}"

    def unprotect(self, protected: str) -> str:
        normalized = str(protected or "").strip()
        if not normalized:
            return ""
        if not normalized.startswith(self.prefix):
            raise ValueError("无法识别保存的 API Key 格式")
        if not self.available:
            raise SecretProtectionUnavailable("当前平台无法解密 Windows DPAPI 密钥")
        encoded = normalized[len(self.prefix):]
        try:
            encrypted = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise ValueError("保存的 API Key 密文已损坏") from error
        return self._crypt(encrypted, decrypt=True).decode("utf-8")

    @staticmethod
    def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
        buffer = ctypes.create_string_buffer(data)
        blob = _DataBlob(
            len(data),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        return blob, buffer

    def _crypt(self, data: bytes, *, decrypt: bool) -> bytes:
        input_blob, input_buffer = self._blob(data)
        output_blob = _DataBlob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        if decrypt:
            success = function(
                ctypes.byref(input_blob),
                None,
                None,
                None,
                None,
                0,
                ctypes.byref(output_blob),
            )
        else:
            success = function(
                ctypes.byref(input_blob),
                "Research Assistant model API key",
                None,
                None,
                None,
                0,
                ctypes.byref(output_blob),
            )
        if not success:
            raise OSError(ctypes.get_last_error(), "Windows DPAPI 操作失败")
        try:
            # 保持输入缓冲区存活到系统调用结束。
            _ = input_buffer
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)


__all__ = ["SecretProtectionUnavailable", "WindowsDpapiProtector"]
