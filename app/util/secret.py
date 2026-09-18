"""凭据（打印机访问代码）的加解密。

## 三层策略，从强到弱

1. **Windows DPAPI**（``dpapi:`` 前缀）—— 绑定当前 Windows 用户，最强，且不需要
   用户做任何事。桌面版走这条。
2. **本机密钥文件 + Fernet**（``fernet:`` 前缀）—— 非 Windows（Linux / Docker /
   NAS）在 ``cryptography`` 可用时走这条：密钥是 32 字节随机数，落在配置目录下的
   ``secret.key``（权限 0600）。也可以用环境变量 ``BAMBU_MONITOR_SECRET`` 指定口令
   （PBKDF2 派生密钥，不落盘，最推荐）或 ``BAMBU_MONITOR_KEY_FILE`` 把密钥文件放到
   别处（Docker secrets / 只读挂载）。
3. **明文**（无前缀）—— 前两条都不可用时（典型是**安卓**：APK 刻意不含
   ``cryptography``，见 ``docs/PACKAGING.md``），退回明文，并且**必须留下提示**。

## 降级行为是有意保留的、但必须可见

* `encrypt_text` 失败时退回明文存储 —— 记录一条**提示**，由 `app/config.py`
  汇总到 ``AppConfig.warnings``，在界面上告知用户，但**不影响保存本身**。
* 在 Windows 上读到以 ``dpapi:`` 开头、却无法解密的字符串（换了机器或换了
  Windows 用户），`decrypt_text` 返回空串 —— 也记**提示**，提醒用户重填
  访问代码。这一样不该阻断添加/保存操作。

## 第 2 层能防住什么、防不住什么（不要夸大）

密钥文件与配置文件在**同一个目录**里，所以它防的是「配置文件被单独复制走」
（截图、粘贴进工单、误提交进 git、备份只备份了 config.json）；
**防不住**能读到该目录的人（包括 root、容器逃逸、整个目录被拷走）。
要更强就用 ``BAMBU_MONITOR_SECRET`` 口令把密钥放到进程环境里。
安卓版把配置放在应用私有目录（``filesDir``），受沙箱保护。

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
import binascii
import ctypes
import hashlib
import hmac
import logging
import os
import secrets as secrets_module
import sys
from ctypes import wintypes

LOGGER = logging.getLogger("bambu-monitor.secret")

_PREFIX = "dpapi:"
#: 非 Windows 的加密前缀（本机密钥文件 / 口令派生密钥 + Fernet）
_PREFIX_FERNET = "fernet:"
#: 口令派生用的固定盐：与配置格式绑定，换它就是换密钥（老配置会解不开）
_KDF_SALT = b"bambu-monitor-config-v1"
_KDF_ROUNDS = 200_000

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

    刻意不记为错误：平台不给加密能力（安卓没有 ``cryptography``）是平台事实，
    不是这次操作失败。访问代码确实已经保存好了，调用方不该据此判定保存失败。
    """
    if not text:
        return ""
    try:
        return _PREFIX + base64.b64encode(_crypt(text.encode("utf-8"), True)).decode("ascii")
    except Exception as exc:
        return _encrypt_without_dpapi(text, exc)


def _encrypt_without_dpapi(text: str, exc: Exception) -> str:
    """DPAPI 不可用时的降级：先试 Fernet（本机密钥），再退回明文。"""
    fernet = _fernet()
    if fernet is not None:
        token = fernet.encrypt(text.encode("utf-8")).decode("ascii")
        if os.environ.get("BAMBU_MONITOR_SECRET", "").strip():
            note = "密钥由环境变量 BAMBU_MONITOR_SECRET 的口令派生，不落盘"
        else:
            note = (
                f"密钥文件是 {_key_path()}（与配置同目录）：这能防住「配置文件被单独复制走」，"
                "防不住能读到该目录的人；需要更强保护可设置环境变量 BAMBU_MONITOR_SECRET 指定口令"
            )
        reason = (
            f"访问代码无法用 DPAPI 加密（{exc}）"
            if sys.platform == "win32"
            else "当前系统没有 DPAPI"
        )
        _remember(f"{reason}，已改用本机密钥加密；{note}", LEVEL_WARNING)
        return _PREFIX_FERNET + token
    if sys.platform == "win32":
        _remember(
            f"访问代码加密失败（{exc}），将以明文保存，请注意配置文件权限",
            LEVEL_WARNING,
        )
    else:
        _remember(
            "当前系统没有 DPAPI，也没有可用的加密库（cryptography），"
            "访问代码将以明文保存（仅提示，不影响使用）",
            LEVEL_WARNING,
        )
    return text


def decrypt_text(text: str) -> str:
    if not text:
        return ""
    if text.startswith(_PREFIX_FERNET):
        return _decrypt_with_fernet(text[len(_PREFIX_FERNET) :])
    if not text.startswith(_PREFIX):
        return text
    try:
        payload = base64.b64decode(text[len(_PREFIX) :])
        return _crypt(payload, False).decode("utf-8", errors="replace")
    except Exception:
        _remember(_UNREADABLE_MESSAGE, LEVEL_WARNING)
        return ""


#: 凭据解不开时的统一提示（DPAPI 换机器/换用户、密钥文件丢失都会用到）
_UNREADABLE_MESSAGE = (
    "有打印机的访问代码解不开（可能是其它 Windows 用户或别的电脑加密的，"
    "也可能是本机密钥文件 secret.key 丢失），请在设置里重新填写该打印机的访问代码"
)


def _decrypt_with_fernet(payload: str) -> str:
    fernet = _fernet()
    if fernet is None:
        _remember(
            "配置里的访问代码是用本机密钥加密的，但当前环境没有加密库（cryptography），"
            "无法解开，请在设置里重新填写访问代码",
            LEVEL_WARNING,
        )
        return ""
    try:
        return fernet.decrypt(payload.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        _remember(_UNREADABLE_MESSAGE, LEVEL_WARNING)
        return ""


# --------------------------------------------------------------------------- 本机密钥


def _key_path() -> str:
    """密钥文件路径：``BAMBU_MONITOR_KEY_FILE`` 优先，否则放配置目录下。"""
    override = os.environ.get("BAMBU_MONITOR_KEY_FILE", "").strip()
    if override:
        return override
    # 延迟导入：app/config.py 会 import 本模块，模块级再反向导入就成环了
    from ..config import config_dir

    return os.path.join(config_dir(), "secret.key")


def _read_or_create_key() -> bytes | None:
    """取 32 字节密钥：环境变量口令派生优先，否则读写密钥文件（不存在就新建）。"""
    passphrase = os.environ.get("BAMBU_MONITOR_SECRET", "").strip()
    if passphrase:
        return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), _KDF_SALT, _KDF_ROUNDS, 32)
    path = _key_path()
    try:
        with open(path, "rb") as handle:
            key = handle.read().strip()
        decoded = base64.urlsafe_b64decode(key)
        if len(decoded) == 32:
            return decoded
        LOGGER.warning("密钥文件内容不是 32 字节，将重新生成：%s", path)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        LOGGER.warning("密钥文件不可读（%s）：%s", path, exc)
        return None

    key = _generate_key()
    if key is None:
        return None
    try:
        # 先用 0600 建文件再写内容：先写后 chmod 会有一个"世界可读"的窗口
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(base64.urlsafe_b64encode(key))
    except OSError as exc:
        LOGGER.warning("密钥文件写入失败（%s）：%s", path, exc)
        return None
    return key


def _generate_key() -> bytes | None:
    import secrets

    return secrets.token_bytes(32)


def _fernet():
    """构造 Fernet 实例；``cryptography`` 不可用或密钥拿不到时返回 None。"""
    try:
        from cryptography.fernet import Fernet
    except Exception:  # noqa: BLE001 - 安卓 APK 刻意不含 cryptography
        return None
    key = _read_or_create_key()
    if key is None:
        return None
    return Fernet(base64.urlsafe_b64encode(key))


def can_encrypt() -> bool:
    """本环境是否能加密凭据（DPAPI 或 Fernet 任一可用）。

    界面/诊断与测试用它判断「凭据是不是必然明文落盘」，避免把平台差异写成
    硬编码的平台判断（安卓的 ``sys.platform`` 也是 ``linux``）。

    注意：**不包含**可移植口令加密（:func:`encrypt_portable`）—— 那条路只用标准库，
    任何平台都能用，所以它不是"本机能不能加密"的判据。
    """
    if sys.platform == "win32":
        return True
    return _fernet() is not None


# --------------------------------------------------------------------------- 可移植口令加密
#
# 目的：**导出的配置文件要在所有版本之间通用**。
#
# 本机加密（DPAPI / secret.key）天然不可移植：Windows 的 dpapi: 换到 Linux 解不开，
# Linux 的 fernet: 换到别的机器也解不开。而用户的需求是「在电脑上导出、在安卓上导入」。
# 所以导出的文件里可以带一份**口令保护**的访问代码。
#
# ## 为什么不用 cryptography 的 Fernet
#
# **安卓版刻意不打包 cryptography**（Chaquopy 的预编译包是 4096 字节对齐，
# 在 16KB 内存页设备上会闪退，见 docs/PACKAGING.md 与 tests/test_android_packaging.py）。
# 用 Fernet 就等于"安卓无法导入" —— 恰好违背这个功能的目的。
# 因此这里只用标准库实现：PBKDF2-HMAC-SHA256 派生密钥 + HMAC-SHA256 计数器流
# （PRF-CTR）+ 先加密后认证（encrypt-then-MAC）。这套组合是标准做法，
# 不是自创算法；容器格式是应用私有的（``bmp1:`` 前缀），不追求与别的软件互通。

_PORTABLE_PREFIX = "bmp1:"
#: PBKDF2 迭代次数：手机上约 100~300ms，够用又不至于让用户等
_PORTABLE_ITERATIONS = 200_000
_PORTABLE_SALT_BYTES = 16
_PORTABLE_NONCE_BYTES = 16


def _portable_keys(passphrase: str, salt: bytes) -> tuple[bytes, bytes]:
    """口令 + 盐 -> (加密密钥, 认证密钥)。"""
    material = hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), salt, _PORTABLE_ITERATIONS, 64
    )
    return material[:32], material[32:]


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """HMAC-SHA256 计数器流（PRF-CTR）：和明文等长的密钥流。"""
    stream = bytearray()
    counter = 0
    while len(stream) < length:
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        stream += block
        counter += 1
    return bytes(stream[:length])


class PortableCipher:
    """一次口令派生、多次加解密。

    为什么要做成对象：PBKDF2 20 万次迭代约 200~400ms，一份配置里可能有十几台设备 ——
    每台都派生一次就是好几秒。所以**一次导出/导入只派生一次**，各条凭据共用一个盐、
    各自带自己的 nonce（`bmp1:<盐>:<nonce>:<密文>:<认证码>`）。

    只依赖标准库（见模块内注释：安卓版没有 `cryptography`）。
    """

    def __init__(self, passphrase: str, salt: bytes | None = None) -> None:
        if not passphrase:
            raise ValueError("口令不能为空")
        self.salt = salt or secrets_module.token_bytes(_PORTABLE_SALT_BYTES)
        self._enc_key, self._mac_key = _portable_keys(passphrase, self.salt)

    @property
    def salt_b64(self) -> str:
        return base64.b64encode(self.salt).decode("ascii")

    def encrypt(self, text: str) -> str:
        if not text:
            return ""
        nonce = secrets_module.token_bytes(_PORTABLE_NONCE_BYTES)
        data = text.encode("utf-8")
        cipher = bytes(
            left ^ right for left, right in zip(data, _keystream(self._enc_key, nonce, len(data)))
        )
        tag = hmac.new(self._mac_key, self.salt + nonce + cipher, hashlib.sha256).digest()
        parts = (self.salt, nonce, cipher, tag)
        return _PORTABLE_PREFIX + ":".join(base64.b64encode(p).decode("ascii") for p in parts)

    def decrypt(self, blob: str) -> str:
        if not blob.startswith(_PORTABLE_PREFIX):
            raise ValueError("不是口令加密的凭据")
        parts = blob[len(_PORTABLE_PREFIX) :].split(":")
        if len(parts) != 4:
            raise ValueError("凭据格式不正确")
        try:
            salt, nonce, cipher, tag = (base64.b64decode(part, validate=True) for part in parts)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("凭据格式不正确") from exc
        if salt != self.salt:
            # 不是同一份文件/同一批口令派生的：直接用本对象的密钥算认证码必然失败，
            # 但这里给出更准确的结论
            raise ValueError("凭据与本文件的盐不一致")
        expected = hmac.new(self._mac_key, salt + nonce + cipher, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, tag):
            # 先认证再解密：口令不对与文件被改过在这里是同一个结论
            raise ValueError("口令不对，或配置文件被修改过")
        data = bytes(
            left ^ right for left, right in zip(cipher, _keystream(self._enc_key, nonce, len(cipher)))
        )
        return data.decode("utf-8")


def encrypt_portable(text: str, passphrase: str) -> str:
    """用口令加密一条凭据（单次使用；多条请复用 :class:`PortableCipher`）。"""
    return PortableCipher(passphrase).encrypt(text)


def decrypt_portable(blob: str, passphrase: str) -> str:
    """解开口令加密的凭据；口令不对或内容被改过时抛 ``ValueError``。"""
    if not blob.startswith(_PORTABLE_PREFIX):
        raise ValueError("不是口令加密的凭据")
    parts = blob[len(_PORTABLE_PREFIX) :].split(":")
    if len(parts) != 4:
        raise ValueError("凭据格式不正确")
    try:
        salt = base64.b64decode(parts[0], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("凭据格式不正确") from exc
    return PortableCipher(passphrase, salt).decrypt(blob)


def is_portable(text: str) -> bool:
    """是否是口令加密的可移植凭据。"""
    return bool(text) and text.startswith(_PORTABLE_PREFIX)


def is_encrypted(text: str) -> bool:
    """是否已加密（本机加密或口令加密）。明文（含安卓降级存储的）返回 False。"""
    return bool(text) and (
        text.startswith(_PREFIX) or text.startswith(_PREFIX_FERNET) or is_portable(text)
    )


def token_hex(length: int = 8) -> str:
    """生成随机访问令牌（网页监控用）。"""
    import secrets

    return secrets.token_hex(max(4, length))
