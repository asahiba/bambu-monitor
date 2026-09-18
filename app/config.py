"""应用配置读写（打印机列表、界面偏好）。

访问代码的存储见 `app/util/secret.py`：Windows 用 DPAPI，其它平台（Linux /
Docker / NAS）在本机密钥可用时用「密钥文件 + Fernet」，都不可用时才退回明文
（安卓 APK 不含 ``cryptography``，属于最后一种）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from .bambu.models import PrinterInfo, PrinterModel
from .util import secret

APP_NAME = "BambuMonitor"
LOGGER = logging.getLogger("bambu-monitor.config")


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

    def to_json(self) -> str:
        """序列化当前配置（访问代码加密后写入，见 `app/util/secret.py`）。"""
        secret.clear_last_error()
        data: dict[str, Any] = {
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
        for printer in self.printers:
            item = asdict(printer)
            item["model"] = printer.model.value
            item["access_code"] = secret.encrypt_text(printer.access_code)
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

    def export_to(self, path: str) -> bool:
        self.last_error = ""
        self.warnings = ""
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.to_json())
        except OSError as exc:
            LOGGER.error("配置导出失败：%s", exc)
            self.last_error = f"配置导出失败：{exc}"
            return False
        warning = secret.last_warning()
        if warning:
            self.warnings = warning
        return True

    def import_from(self, path: str) -> bool:
        """从导出的配置里恢复设置（访问代码需为同一 Windows 用户加密的）。

        这里以前只恢复 printers/columns/max_fps/refresh_ms/web_*，
        而 ``to_json()`` 写出的 ``last_timeout`` / ``show_timestamp`` /
        ``auto_connect`` / ``web_enabled`` 在导入后被**静默丢弃** ——
        用户「导出再导入」后界面偏好并没有跟着回来，看起来像导入了一半。

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
        try:
            loaded = _parse(data)
        except Exception as exc:  # noqa: BLE001 - 导入外部文件更不能让程序崩
            LOGGER.error("配置导入内容不可用：%s", exc)
            self.last_error = f"配置导入失败：{exc}"
            return False
        if not loaded.printers:
            return False
        # 导入成功；原文件里的凭据解不开只是提示（用户重填即可），不算导入失败
        self.warnings = loaded.warnings
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
