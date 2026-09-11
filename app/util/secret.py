"""Windows DPAPI 凭据加解密（加密后保存访问代码，避免明文落盘）。

**降级行为是有意保留的、但必须可见**：

* 非 Windows（Linux / Docker）没有 DPAPI，`encrypt_text` 会失败。为了让这些平台
  仍能保存访问代码，这里退回明文存储 —— 但会记录一条诊断信息，由 `app/config.py`
  汇总到 ``AppConfig.last_error`` 并在界面上提示，避免用户误以为凭据已加密。
* 在 Windows 上读到以 ``dpapi:`` 开头、却无法解密的字符串（换机器/换 Windows 用户），
  `decrypt_text` 返回空串，同样会留下一诊断信息，否则表现只是「画面不出来」，无从排查。
"""

from __future__ import annotations

import base64
import ctypes
import logging
import sys
from ctypes import wintypes

LOGGER = logging.getLogger("bambu-monitor.secret")

_PREFIX = "dpapi:"

#: 最近一次降级/失败的说明（供界面与日志展示）；没有问题时为 None
_LAST_ERROR: str | None = None


def _remember(message: str) -> None:
    """记录一条诊断信息（同时写日志，便于事后排查）。"""
    global _LAST_ERROR
    _LAST_ERROR = message
    LOGGER.warning("%s", message)


def last_error() -> str | None:
    """返回最近一次加密/解密降级的说明；正常时为 None。"""
    return _LAST_ERROR


def clear_last_error() -> None:
    """清空诊断状态（每次读写配置前调用，保证报告的是本次结果）。"""
    global _LAST_ERROR
    _LAST_ERROR = None


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
    """加密字符串；失败时原样返回，但会记录诊断信息（调用方仍可正常使用）。"""
    if not text:
        return ""
    try:
        return _PREFIX + base64.b64encode(_crypt(text.encode("utf-8"), True)).decode("ascii")
    except Exception as exc:
        if sys.platform == "win32":
            _remember(f"访问代码加密失败（{exc}），将以明文保存，请注意配置文件权限")
        else:
            _remember("当前系统没有 DPAPI，访问代码将以明文保存在配置文件里，请注意目录权限")
        return text


def decrypt_text(text: str) -> str:
    if not text:
        return ""
    if not text.startswith(_PREFIX):
        return text
    try:
        payload = base64.b64decode(text[len(_PREFIX) :])
        return _crypt(payload, False).decode("utf-8", errors="replace")
    except Exception as exc:
        _remember(
            f"访问代码解密失败（{exc}）：该配置可能是其它 Windows 用户/其它电脑加密的，"
            "请重新填写这台打印机的访问代码"
        )
        return ""


def is_encrypted(text: str) -> bool:
    return bool(text) and text.startswith(_PREFIX)


def token_hex(length: int = 8) -> str:
    """生成随机访问令牌（网页监控用）。"""
    import secrets

    return secrets.token_hex(max(4, length))
