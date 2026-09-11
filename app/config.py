"""应用配置读写（打印机列表、界面偏好）。访问代码使用 DPAPI 加密存储。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from .bambu.models import PrinterInfo, PrinterModel
from .util import secret

APP_NAME = "BambuMonitor"


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


def _parse(data: dict[str, Any]) -> "AppConfig":
    """把 JSON 数据转换成 AppConfig（load 与 import 共用）。"""
    printers: list[PrinterInfo] = []
    for item in data.get("printers", []):
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
                tile_span=max(1, min(3, int(item.get("tile_span", 1) or 1))),
            )
        )
    web_token = str(data.get("web_token", "") or "") or secret.token_hex(8)
    return AppConfig(
        printers=printers,
        columns=int(data.get("columns", 0) or 0),
        window_geometry=str(data.get("window_geometry", "")),
        show_timestamp=bool(data.get("show_timestamp", True)),
        auto_connect=bool(data.get("auto_connect", True)),
        last_timeout=max(15.0, min(60.0, float(data.get("last_timeout", 20.0) or 20.0))),
        max_fps=max(0.0, min(30.0, float(data.get("max_fps", 10.0) or 10.0))),
        refresh_ms=max(50, min(1000, int(data.get("refresh_ms", 150) or 150))),
        web_enabled=bool(data.get("web_enabled", False)),
        web_port=max(1, min(65535, int(data.get("web_port", 8080) or 8080))),
        web_token=web_token,
        web_fps=max(0.5, min(15.0, float(data.get("web_fps", 4.0) or 4.0))),
        web_max_width=max(240, min(1920, int(data.get("web_max_width", 720) or 720))),
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

    # ------------------------------------------------------------------ 读写
    @classmethod
    def load(cls) -> "AppConfig":
        path = config_path()
        for candidate in (path, backup_path()):
            if not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            if candidate != path:
                print(f"[config] 主配置不可用，已从备份恢复：{candidate}")
            return _parse(data)
        return cls()

    def to_json(self) -> str:
        """序列化当前配置（访问代码用 DPAPI 加密，只有本机当前用户能解密）。"""
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
        if not self.persist:
            return
        path = config_path()
        # 先备份上一份，避免意外写坏或误删导致配置丢失
        try:
            if os.path.exists(path):
                import shutil

                shutil.copyfile(path, backup_path())
        except OSError:
            pass
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(self.to_json())
            os.replace(tmp, path)
        except OSError:
            pass

    def export_to(self, path: str) -> bool:
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.to_json())
            return True
        except OSError:
            return False

    def import_from(self, path: str) -> bool:
        """从导出的配置里恢复打印机列表（访问代码需为同一 Windows 用户加密的）。"""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return False
        loaded = _parse(data)
        if not loaded.printers:
            return False
        self.printers = loaded.printers
        self.columns = loaded.columns
        self.max_fps = loaded.max_fps
        self.refresh_ms = loaded.refresh_ms
        self.web_port = loaded.web_port
        self.web_token = loaded.web_token or self.web_token
        self.web_fps = loaded.web_fps
        self.web_max_width = loaded.web_max_width
        return True
