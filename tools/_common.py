"""开发脚本共用小工具：从配置里按 IP 查访问代码（避免口令出现在命令行/日志里）。"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import config_path  # noqa: E402
from app.util import secret  # noqa: E402


def lookup_code(ip: str) -> str:
    path = config_path()
    if not os.path.exists(path):
        return ""
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    for item in data.get("printers", []):
        if str(item.get("ip", "")).strip() == ip.strip():
            return secret.decrypt_text(str(item.get("access_code", "")))
    return ""


def lookup_serial(ip: str) -> str:
    path = config_path()
    if not os.path.exists(path):
        return ""
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    for item in data.get("printers", []):
        if str(item.get("ip", "")).strip() == ip.strip():
            return str(item.get("serial", ""))
    return ""


def resolve_code(ip: str, argument: str = "") -> str:
    """``--auto`` 或空参数时从配置文件读取该 IP 的访问代码。"""
    if argument and argument != "--auto":
        return argument
    return lookup_code(ip)


def enable_utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def use_temp_config_dir(prefix: str = "bambu-test-") -> str:
    """把配置目录切到临时目录，**任何会写配置的测试都必须先调用它**。

    不这样做的话，测试会写到用户真实的 ``%APPDATA%\\BambuMonitor\\config.json``
    （曾经因此把真实配置覆盖成测试数据，靠自动备份才恢复）。
    """
    import tempfile

    path = tempfile.mkdtemp(prefix=prefix)
    os.environ["BAMBU_MONITOR_CONFIG_DIR"] = path
    # 立即校验，确保后续 import 拿到的是临时目录
    from app.config import config_path  # 延迟导入，确保环境变量先生效

    resolved = config_path()
    if not os.path.abspath(resolved).startswith(os.path.abspath(path)):
        raise RuntimeError(f"配置目录未切换到临时目录，已中止：{resolved}")
    return path
