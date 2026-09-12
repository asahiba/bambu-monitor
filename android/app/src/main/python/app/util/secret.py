"""Windows DPAPI 凭据加解密（加密后保存访问代码，避免明文落盘）。

**降级行为是有意保留的、但必须可见**：

* 非 Windows（Linux / Docker / **安卓**）没有 DPAPI，`encrypt_text` 会失败。
  为了让这些平台仍能保存访问代码，这里退回明文存储 —— 记录一条**提示**，
  由 `app/config.py` 汇总到 ``AppConfig.warnings``，在界面上告知用户
  「凭据是明文」，但**不影响保存本身**。
* 在 Windows 上读到以 ``dpapi:`` 开头、却无法解密的字符串（换了机器或换了
  Windows 用户），`decrypt_text` 返回空串 —— 也记**提示**，提醒用户重填
  访问代码。这一样不该阻断添加/保存操作。

## 为什么分成「错误」和「提示」两级

这里曾经只有一条通道，于是「本平台没有 DPAPI，已按明文保存」这种
**正常的、无法避免的**降级被当成错误上报，``AppConfig.last_error`` 因此非空。
而调用方（``app/web/host.py`` 的添加设备）看到 ``last_error`` 非空就判定
「添加失败」—— 在安卓上表现为：**设备其实已经加进去了，界面却报
「添加失败当前系统没有 DPAPI」**，用户以为没加上，反复重试。

所以现在：

* :data:`LEVEL_WARNING` —— 提示，不阻断（明文保存、凭据解不开、读配置时的降级）；
* :data:`LEVEL_ERROR` —— 真错误，阻断（**写盘失败**，配置没保存成功）。

关键判据是**「这次操作到底成没成」**：写盘失败是失败；凭据只能明文存则是成功。
`app/web/host.py` 只把 ``last_error`` 当失败。
"""

from __future__ import annotations

import base64
import ctypes
import logging
import sys
from ctypes import wintypes

LOGGER = logging.getLogger("bambu-monitor.secret")

_PREFIX = "dpapi:"

#: 诊断级别：提示（不阻断） / 错误（阻断）
LEVEL_WARNING = "warning"
LEVEL_ERROR = "error"

#: 两条通道互相独立：一次操作可能既写盘失败、又有若干提示
_LAST_ERROR: str | None = None
_LAST_WARNINGS: list[str] = []


def _remember(message: str, level: str = LEVEL_WARNING) -> None:
    """记录一条诊断信息（同时写日志，便于事后排查）。"""
    global _LAST_ERROR
    if level == LEVEL_ERROR:
        _LAST_ERROR = message
    elif message not in _LAST_WARNINGS:
        _LAST_WARNINGS.append(message)
    LOGGER.warning("%s", message)


def last_error() -> str | None:
    """返回最近一次**错误**级诊断；只有提示或一切正常时为 None。

    调用方用它判断"这次保存是不是真的失败了"，所以**提示级信息绝不能**从这里
    返回 —— 否则安卓上的明文降级会把一次成功的添加报成失败。
    """
    return _LAST_ERROR


def last_warning() -> str | None:
    """把本次累积的提示合成一句话；没有提示时为 None。"""
    if not _LAST_WARNINGS:
        return None
    return "；".join(_LAST_WARNINGS)


def clear_last_error() -> None:
    """清空诊断状态（每次读写配置前调用，保证报告的是本次结果）。"""
    global _LAST_ERROR
    _LAST_ERROR = None
    _LAST_WARNINGS.clear()


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
    """加密字符串；无法加密时原样返回，并记录一条**提示**。

    刻意不记为错误：非 Windows 平台没有 DPAPI 是平台事实，不是这次操作失败。
    访问代码确实已经（以明文）保存好了，调用方不该据此判定保存失败。
    """
    if not text:
        return ""
    try:
        return _PREFIX + base64.b64encode(_crypt(text.encode("utf-8"), True)).decode("ascii")
    except Exception as exc:
        if sys.platform == "win32":
            _remember(
                f"访问代码加密失败（{exc}），将以明文保存，请注意配置文件权限",
                LEVEL_WARNING,
            )
        else:
            _remember(
                "当前系统没有 DPAPI，访问代码将以明文保存（仅提示，不影响使用）",
                LEVEL_WARNING,
            )
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
        _remember(
            "有打印机的访问代码解不开（可能是其它 Windows 用户或别的电脑加密的），"
            "请在设置里重新填写该打印机的访问代码",
            LEVEL_WARNING,
        )
        return ""


def is_encrypted(text: str) -> bool:
    return bool(text) and text.startswith(_PREFIX)


def token_hex(length: int = 8) -> str:
    """生成随机访问令牌（网页监控用）。"""
    import secrets

    return secrets.token_hex(max(4, length))
