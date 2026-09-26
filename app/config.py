"""应用配置读写（打印机列表、界面偏好）。

访问代码的存储见 `app/util/secret.py`：Windows 用 DPAPI，其它平台（Linux /
Docker / NAS）在本机密钥可用时用「密钥文件 + Fernet」，都不可用时才退回明文
（安卓 APK 不含 ``cryptography``，属于最后一种）。

## 导出的配置文件为什么要有「口令」这一档

本机加密天然**不可移植**：Windows 导出的 `dpapi:` 在 Linux/安卓上解不开，
Linux 导出的 `fernet:` 换台机器也解不开。于是「在电脑上导出、在安卓上导入」
这条最自然的用法会丢掉全部访问代码。

所以导出时可以选择**用口令保护**（`app/util/secret.py` 的 :class:`PortableCipher`，
只用标准库，安卓也能解），导出的文件在任意版本、任意平台都能导入：

| 导出方式 | 文件里的访问代码 | 能在哪里导入 |
| --- | --- | --- |
| 不带口令（旧行为） | 本机加密（`dpapi:` / `fernet:`）或明文 | **同一台机器/同一用户**；跨平台只能重新填访问代码 |
| 带口令（推荐） | `bmp1:` 口令加密 | **任何平台、任何版本**（Windows / Linux / Docker / 安卓） |
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from .bambu.models import PrinterInfo, PrinterModel
from .util import secret

APP_NAME = "BambuMonitor"
LOGGER = logging.getLogger("bambu-monitor.config")

#: 导出文件的格式版本。1（或没有该字段）= 老格式；2 = 带 ``format``/``portable`` 标记
EXPORT_FORMAT = 2

#: 设备凭据字段 —— **凡是凭据都要走同一套加密与"解不开就逐个点名"的流程**。
#: `access_code` 是拓竹的访问代码，`api_key` 是第三方族（Moonraker 等）的 API Key。
#: 新增凭据字段时只改这一处，导出/导入/加密三条路径都不会漏。
CREDENTIAL_FIELDS: tuple[str, ...] = ("access_code", "api_key")


def _new_token() -> str:
    return secret.token_hex(8)


def config_dir() -> str:
    """配置目录：可用环境变量 BAMBU_MONITOR_CONFIG_DIR 覆盖（Docker/多实例用）。"""
    override = os.environ.get("BAMBU_MONITOR_CONFIG_DIR", "").strip()
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def backup_path() -> str:
    return os.path.join(config_dir(), "config.backup.json")


def _coerce_int(data: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    """读取一个整数配置项并夹到 [low, high]；类型不对时退回默认值（而不是抛异常）。"""
    try:
        return max(low, min(high, int(float(data.get(key, default)))))
    except (TypeError, ValueError):
        LOGGER.warning("配置项 %s 的值无法解析为整数，已使用默认值 %s", key, default)
        return default


def _coerce_float(data: dict[str, Any], key: str, default: float, low: float = 0.0, high: float = 0.0) -> float:
    """读取一个浮点配置项；``high`` 为 0.0 表示只做下界检查。

    注意**不能**用 ``x or default`` 的写法：那会把合法的 ``0`` 当成「没填」，
    导致「max_fps = 0（不限制帧率）」永远存不下来（见 test_config 的回归用例）。
    """
    try:
        value = float(data.get(key, default))
    except (TypeError, ValueError):
        LOGGER.warning("配置项 %s 的值无法解析为数值，已使用默认值 %s", key, default)
        return default
    if value < low:
        return low
    return min(high, value) if high > 0.0 else value


def _parse(data: dict[str, Any]) -> "AppConfig":
    """把 JSON 数据转换成 AppConfig（load 与 import 共用）。

    对「合法 JSON 但内容不可用」的文件要优雅降级：根不是对象、字段类型不对
    （例如 ``tile_span: "auto"``、``web_port: "http"``）都不应该让程序起不来，
    而是回落到默认值。``AppConfig.load()`` 会在本函数抛异常时改试备份文件。
    """
    secret.clear_last_error()
    if not isinstance(data, dict):
        raise ValueError(f"配置根节点应为对象，实际是 {type(data).__name__}")
    printers: list[PrinterInfo] = []
    raw_printers = data.get("printers", [])
    if not isinstance(raw_printers, list):
        # 结构损坏（例如被写成对象）：不整体放弃，至少保住与界面相关的偏好设置
        LOGGER.warning("配置里的 printers 字段不是数组，已忽略")
        raw_printers = []
    for item in raw_printers:
        if not isinstance(item, dict):
            continue
        try:
            model = PrinterModel(item.get("model", "未知机型"))
        except ValueError:
            model = PrinterModel.UNKNOWN
        printers.append(
            PrinterInfo(
                ip=str(item.get("ip", "")),
                serial=str(item.get("serial", "")),
                name=str(item.get("name", "")),
                model=model,
                firmware=str(item.get("firmware", "")),
                access_code=secret.decrypt_text(str(item.get("access_code", ""))),
                stream_mode=str(item.get("stream_mode", "auto")),
                tile_span=_coerce_int(item, "tile_span", 1, 1, 3),
                # 以前只有 to_json() 写出 discovered、_parse() 从不读回，
                # 「导出再导入」会把它悄悄变成 False（往返不保真）。
                discovered=bool(item.get("discovered", False)),
                # 设备族：**空字符串 = 拓竹**（老配置里没有这个字段）
                family=str(item.get("family", "") or ""),
                port=_coerce_int(item, "port", 0, 0, 65535),
                # 非拓竹族的凭据，与访问代码同样按本机方式加密保存
                api_key=secret.decrypt_text(str(item.get("api_key", ""))),
                camera_url=str(item.get("camera_url", "") or ""),
                camera_index=_coerce_int(item, "camera_index", 0, 0, 99),
                # 第三方族的开/关灯 G-code（用户自己填，见 PrinterInfo 的说明）
                light_on_gcode=str(item.get("light_on_gcode", "") or ""),
                light_off_gcode=str(item.get("light_off_gcode", "") or ""),
            )
        )
    web_token = str(data.get("web_token", "") or "") or secret.token_hex(8)
    return AppConfig(
        printers=printers,
        columns=_coerce_int(data, "columns", 0, 0, 999),
        window_geometry=str(data.get("window_geometry", "")),
        show_timestamp=bool(data.get("show_timestamp", True)),
        auto_connect=bool(data.get("auto_connect", True)),
        last_timeout=_coerce_float(data, "last_timeout", 20.0, 15.0, 60.0),
        # max_fps = 0 表示「不限制帧率」，是合法取值，必须原样保留
        max_fps=_coerce_float(data, "max_fps", 10.0, 0.0, 30.0),
        refresh_ms=_coerce_int(data, "refresh_ms", 150, 50, 1000),
        web_enabled=bool(data.get("web_enabled", False)),
        web_port=_coerce_int(data, "web_port", 8080, 1, 65535),
        web_token=web_token,
        web_fps=_coerce_float(data, "web_fps", 4.0, 0.5, 15.0),
        web_max_width=_coerce_int(data, "web_max_width", 720, 240, 1920),
        # 凭据解密若有降级（例如换了 Windows 用户），在这里一并报给调用方
        last_error=secret.last_error() or "",
        warnings=secret.last_warning() or "",
    )


@dataclass
class AppConfig:
    printers: list[PrinterInfo] = field(default_factory=list)
    columns: int = 0  # 0 = 自适应
    window_geometry: str = ""
    show_timestamp: bool = True
    auto_connect: bool = True
    #: 单轮搜索时长。实测打印机对 SSDP 探测有节流（不是每次都回应）：
    #: 12 秒大约 8-11 台、15 秒 10-13 台、20-30 秒可稳定搜全部。
    last_timeout: float = 20.0
    #: 每路画面的最大帧率（0 = 不限制；RTSPS 通道实际按此节流并在目标尺寸编码）
    max_fps: float = 10.0
    #: 界面刷新间隔（毫秒）
    refresh_ms: int = 150
    #: 网页监控
    web_enabled: bool = False
    web_port: int = 8080
    #: 访问令牌：新配置自动生成，并在启动时落盘，保证重启后网页地址不变
    web_token: str = field(default_factory=_new_token)
    web_fps: float = 4.0
    web_max_width: int = 720
    #: 为 False 时不写盘（演示/测试模式）
    persist: bool = True
    #: 最近一次读写配置的**失败**说明（保存失败、导出失败、导入失败…）；
    #: 空字符串表示没有问题。调用方据此判定"操作是不是真的失败了"。
    last_error: str = ""
    #: 最近一次读写配置的**提示**（不阻断操作）：例如「本平台没有 DPAPI，已改用
    #: 本机密钥加密」「访问代码只能明文保存」「某台打印机的访问代码解不开，请重填」。
    #: 与 last_error 分开是必须的：把提示当错误会让安卓上成功的添加被报成失败。
    warnings: str = ""

    # ------------------------------------------------------------------ 读写
    @classmethod
    def load(cls) -> "AppConfig":
        """读配置；主配置任何一种损坏都回退到备份，再不行就用默认值。

        「损坏」不止是 JSON 语法错——根节点不是对象、字段类型不对（``tile_span``
        是字符串等）同样属于损坏。以前这些异常会**直接抛出**，让程序在启动时
        崩掉，与 README「主配置损坏时会自动从备份恢复」的承诺不符。
        """
        path = config_path()
        last_problem = ""
        for candidate in (path, backup_path()):
            if not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                last_problem = f"读取失败：{exc}"
                LOGGER.warning("配置文件不可用（%s）：%s", candidate, exc)
                continue
            try:
                config = _parse(data)
            except Exception as exc:  # noqa: BLE001 - 任何解析异常都不该让程序起不来
                last_problem = f"内容不可用：{exc}"
                LOGGER.warning("配置文件内容不可用（%s）：%s", candidate, exc)
                continue
            if candidate != path:
                LOGGER.info("主配置不可用，已从备份恢复：%s", candidate)
                print(f"[config] 主配置不可用，已从备份恢复：{candidate}")
                if not config.last_error:
                    config.last_error = "主配置损坏，已从备份恢复"
            return config
        fallback = cls()
        if last_problem:
            fallback.last_error = f"配置无法读取（{last_problem}），已使用默认设置"
        return fallback

    def to_json(self, passphrase: str = "") -> str:
        """序列化当前配置。

        :param passphrase: 非空时，访问代码改用**口令加密**（`bmp1:` 前缀）——
            这样导出的文件在**任何版本、任何平台**都能导入（Windows / Linux /
            Docker / 安卓）。留空则按本机方式加密（旧行为，只在本机能解）。
        """
        secret.clear_last_error()
        portable = bool(passphrase)
        data: dict[str, Any] = {
            "format": EXPORT_FORMAT if portable else 1,
            "portable": portable,
            "printers": [],
            "columns": self.columns,
            "window_geometry": self.window_geometry,
            "show_timestamp": self.show_timestamp,
            "auto_connect": self.auto_connect,
            "last_timeout": self.last_timeout,
            "max_fps": self.max_fps,
            "refresh_ms": self.refresh_ms,
            "web_enabled": self.web_enabled,
            "web_port": self.web_port,
            "web_token": self.web_token,
            "web_fps": self.web_fps,
            "web_max_width": self.web_max_width,
        }
        # 一份文件里可能有十几台设备：只派生一次密钥（PBKDF2 20 万次要几百毫秒）
        cipher = secret.PortableCipher(passphrase) if portable else None
        for printer in self.printers:
            item = asdict(printer)
            item["model"] = printer.model.value
            # 两种凭据（拓竹的访问代码、第三方族的 API Key）走同一套加密
            for key in CREDENTIAL_FIELDS:
                value = str(getattr(printer, key, "") or "")
                item[key] = cipher.encrypt(value) if cipher else secret.encrypt_text(value)
            data["printers"].append(item)
        return json.dumps(data, ensure_ascii=False, indent=2)

    def save(self) -> None:
        """写盘；失败原因记录在 ``last_error``（不再静默吞掉）。

        注意区分两条通道（登录见 `app/util/secret.py` 的模块文档）：

        * ``last_error`` —— **写盘真的失败了**，调用方应当判定操作失败；
        * ``warnings``   —— 凭据只能明文存之类的提示，操作本身是成功的。

        以前把后者也塞进 ``last_error``，导致安卓上「添加成功」被报成
        「添加失败当前系统没有 DPAPI」。
        """
        if not self.persist:
            return
        self.last_error = ""
        self.warnings = ""
        path = config_path()
        # 先备份上一份，避免意外写坏或误删导致配置丢失
        try:
            if os.path.exists(path):
                import shutil

                shutil.copyfile(path, backup_path())
        except OSError as exc:
            LOGGER.warning("配置备份失败：%s", exc)
            self.last_error = f"配置备份失败：{exc}"
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(self.to_json())
            os.replace(tmp, path)
        except OSError as exc:
            # 这里以前是完全静默的：用户看到「已保存」，实际配置没落盘
            LOGGER.error("配置保存失败：%s", exc)
            self.last_error = f"配置保存失败：{exc}"
            return
        # 凭据加密若有降级（例如安卓连 cryptography 都没有），只作为提示告知用户，
        # **不**影响"保存成功"这个结论
        warning = secret.last_warning()
        if warning:
            self.warnings = warning

    def export_to(self, path: str, passphrase: str = "") -> bool:
        """导出到文件。

        :param passphrase: 非空时用口令保护访问代码（**跨版本/跨平台可导入**）；
            留空则按本机方式加密 —— 那种文件换机器/换用户后访问代码要重填。
        """
        self.last_error = ""
        self.warnings = ""
        try:
            payload = self.to_json(passphrase)
        except ValueError as exc:  # 例如口令为空字符串之外的问题
            self.last_error = f"配置导出失败：{exc}"
            return False
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(payload)
        except OSError as exc:
            LOGGER.error("配置导出失败：%s", exc)
            self.last_error = f"配置导出失败：{exc}"
            return False
        warning = secret.last_warning()
        if warning:
            self.warnings = warning
        return True

    @staticmethod
    def needs_passphrase(path: str) -> bool:
        """这个导出文件是不是**用口令加密**的（导入前据此决定要不要问口令）。

        读不到或不是 JSON 时返回 False —— 真正的错误留给 :meth:`import_from` 报。
        """
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return False
        return _is_portable_file(data)

    def import_from(self, path: str, passphrase: str = "") -> bool:
        """从导出的配置里恢复设置。

        ## 三种文件都能导入

        * **口令加密**（``portable: true``）：必须给对 ``passphrase``，
          任何平台/版本都能解（Windows / Linux / Docker / 安卓）；
        * **本机加密**（`dpapi:` / `fernet:`）：只有本机能解 —— 解不开的设备会被
          逐个点名，并提示「下次导出时带口令」；
        * **明文**：原样导入（安卓的降级存储、以及 `export_plain_config.py` 的产物）。

        ## 以前的问题

        这里只恢复 printers/columns/max_fps/refresh_ms/web_*，而 ``to_json()``
        写出的 ``last_timeout`` / ``show_timestamp`` / ``auto_connect`` /
        ``web_enabled`` 在导入后被**静默丢弃** —— 用户「导出再导入」后界面偏好
        并没有跟着回来，看起来像导入了一半。

        ``window_geometry`` 仍然**不导入**：那是屏幕坐标，从 4K 机器导到小屏
        笔记本上会把窗口恢复到屏幕外（用户看到的是「导入配置后程序打不开了」）。
        """
        self.last_error = ""
        self.warnings = ""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.error("配置导入失败：%s", exc)
            self.last_error = f"配置导入失败：{exc}"
            return False

        undecryptable: list[str] = []
        if _is_portable_file(data):
            if not passphrase:
                self.last_error = (
                    "这个配置文件是用口令加密的：请在导入时输入当初导出所用的口令"
                )
                return False
            data, undecryptable, total = _decrypt_portable_printers(data, passphrase)
            if total and len(undecryptable) == total:
                # 全部解不开 = 口令不对（或文件被改过），不要静默导入成空凭据
                self.last_error = "口令不对，或配置文件被修改过，无法导入"
                return False
        else:
            undecryptable = _unreadable_local_codes(data)

        try:
            loaded = _parse(data)
        except Exception as exc:  # noqa: BLE001 - 导入外部文件更不能让程序崩
            LOGGER.error("配置导入内容不可用：%s", exc)
            self.last_error = f"配置导入失败：{exc}"
            return False
        if not loaded.printers:
            return False
        # 导入成功；原文件里的凭据解不开只是提示（用户重填即可），不算导入失败
        self.warnings = _join_warnings(loaded.warnings, _undecryptable_hint(undecryptable, data))
        self.printers = loaded.printers
        self.columns = loaded.columns
        self.show_timestamp = loaded.show_timestamp
        self.auto_connect = loaded.auto_connect
        self.last_timeout = loaded.last_timeout
        self.max_fps = loaded.max_fps
        self.refresh_ms = loaded.refresh_ms
        # web_token 同理不强制覆盖：本机已有的令牌是手机书签里记着的地址，
        # 导入别处的配置不应该把它换掉（为空时才采用导入值）。
        self.web_enabled = loaded.web_enabled
        self.web_port = loaded.web_port
        self.web_token = loaded.web_token or self.web_token
        self.web_fps = loaded.web_fps
        self.web_max_width = loaded.web_max_width
        return True


def _is_portable_file(data: Any) -> bool:
    """是不是「口令加密」的导出文件。"""
    if not isinstance(data, dict) or not data.get("portable"):
        return False
    try:
        return int(data.get("format", 1) or 1) >= EXPORT_FORMAT
    except (TypeError, ValueError):
        return True  # 有 portable 标记但 format 写坏了：按口令加密处理更安全


def _portable_salt(blob: str) -> bytes | None:
    """从一条 ``bmp1:盐:nonce:密文:认证码`` 里取出盐。"""
    parts = blob.split(":")
    if len(parts) != 5:
        return None
    try:
        return base64.b64decode(parts[1], validate=True)
    except (ValueError, binascii.Error):
        return None


def _decrypt_portable_printers(data: dict, passphrase: str) -> tuple[dict, list[str], int]:
    """把 ``data`` 里口令加密的凭据解开（访问代码与 API Key 都算）。

    :returns: ``(新的 data, 解不开的设备名, 口令加密的凭据总数)``

    一份文件只派生一次密钥（同一把盐），所以先拿第一条凭据的盐建 cipher；
    盐不一致说明文件被拼过，那条会解不开并被告知。
    """
    result = copy.deepcopy(data)
    printers = result.get("printers")
    if not isinstance(printers, list):
        return result, [], 0
    blobs = [
        (item, key, str(item.get(key, "")))
        for item in printers
        if isinstance(item, dict)
        for key in CREDENTIAL_FIELDS
        if secret.is_portable(str(item.get(key, "")))
    ]
    if not blobs:
        return result, [], 0
    salt = _portable_salt(blobs[0][2])
    if salt is None:
        return (
            result,
            [str(item.get("name") or item.get("ip") or "?") for item, _key, _blob in blobs],
            len(blobs),
        )
    cipher = secret.PortableCipher(passphrase, salt)
    failed: list[str] = []
    for item, key, blob in blobs:
        try:
            item[key] = cipher.decrypt(blob)
        except ValueError:
            item[key] = ""
            failed.append(str(item.get("name") or item.get("ip") or "?"))
    return result, failed, len(blobs)


def _unreadable_local_codes(data: dict) -> list[str]:
    """找出「文件里有凭据、但本机解不开」的设备名（换机器/换用户的典型症状）。"""
    printers = data.get("printers") if isinstance(data, dict) else None
    if not isinstance(printers, list):
        return []
    names: list[str] = []
    for item in printers:
        if not isinstance(item, dict):
            continue
        for key in CREDENTIAL_FIELDS:
            raw = str(item.get(key, ""))
            if raw and secret.is_encrypted(raw) and not secret.decrypt_text(raw):
                names.append(str(item.get("name") or item.get("ip") or "?"))
                break
    return names


def _undecryptable_hint(names: list[str], data: dict) -> str:
    """凭据解不开时给用户的话（要说清原因与**下次怎么做**）。"""
    if not names:
        return ""
    shown = "、".join(names[:5]) + ("…" if len(names) > 5 else "")
    return (
        f"有 {len(names)} 台设备的访问代码在本机解不开（{shown}）："
        "它们是**在别的机器或别的用户下加密**的，需要在设置里重新填写。\n"
        "下次导出时请输入一个口令（导出文件就会带口令保护），"
        "带口令的配置文件可以在任何版本、任何平台导入（Windows / Linux / Docker / 安卓）。"
    )


def _join_warnings(*parts: str) -> str:
    return "；".join(part for part in parts if part)
