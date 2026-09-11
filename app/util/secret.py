"""Windows DPAPI 凭据加解密（加密后保存访问代码，避免明文落盘）。"""

from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes

_PREFIX = "dpapi:"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_to_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _free_blob(blob: _DataBlob) -> None:
    try:
        ctypes.windll.kernel32.LocalFree(blob.pbData)
    except Exception:
        pass


def _crypt(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI 仅在 Windows 上可用")
    crypt32 = ctypes.windll.crypt32
    blob_in = _DataBlob(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    flags = 0x01  # CRYPTPROTECT_UI_FORBIDDEN
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, flags, ctypes.byref(blob_out)
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, flags, ctypes.byref(blob_out)
        )
    if not ok:
        raise OSError(f"CryptProtectData/CryptUnprotectData 调用失败（错误码 {ctypes.GetLastError()}）")
    try:
        return _blob_to_bytes(blob_out)
    finally:
        _free_blob(blob_out)


def encrypt_text(text: str) -> str:
    """加密字符串；失败时原样返回（调用方仍可正常使用）。"""
    if not text:
        return ""
    try:
        return _PREFIX + base64.b64encode(_crypt(text.encode("utf-8"), True)).decode("ascii")
    except Exception:
        return text


def decrypt_text(text: str) -> str:
    if not text:
        return ""
    if not text.startswith(_PREFIX):
        return text
    try:
        payload = base64.b64decode(text[len(_PREFIX) :])
        return _crypt(payload, False).decode("utf-8", errors="replace")
    except Exception:
        return ""


def is_encrypted(text: str) -> bool:
    return bool(text) and text.startswith(_PREFIX)


def token_hex(length: int = 8) -> str:
    """生成随机访问令牌（网页监控用）。"""
    import secrets

    return secrets.token_hex(max(4, length))
