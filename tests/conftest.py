"""pytest 公共夹具（纯逻辑层测试专用）。

三条硬约束在这里统一兜住：

1. **不碰用户真实配置**：``BAMBU_MONITOR_CONFIG_DIR`` 被强制指向 ``tmp_path``，
   见 ``app/config.py:22`` 的覆盖点；
2. **完全离线**：测试期间任何 ``connect`` / ``bind`` / ``listen`` 都被拦截，
   不可能连到真实打印机，也不会占用 322 / 6000 / 8080 / 8883 等端口；
3. **可重复**：不依赖公网、不依赖时间以外的外部状态。
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

#: 项目根目录（tests/ 的上一级）；插入 sys.path 以便 `import app`，
#: 使 pytest 从任意工作目录启动都能收集本目录。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

#: 真实配置目录名（``%APPDATA%\\BambuMonitor``），测试期间绝不允许落到这里
REAL_CONFIG_DIR_NAME = "BambuMonitor"


def real_config_dir() -> Path:
    """用户真实配置目录（仅用于断言「测试没有碰它」，测试不会往里写东西）。"""
    base = os.environ.get("APPDATA") or os.fspath(Path.home())
    return Path(base) / REAL_CONFIG_DIR_NAME


@pytest.fixture(autouse=True)
def isolated_config_dir(tmp_path, monkeypatch) -> Path:
    """把配置目录隔离到 tmp_path。

    契约：``app.config.config_dir()`` 由环境变量 ``BAMBU_MONITOR_CONFIG_DIR`` 决定；
    测试期间它必须指向临时目录，且**绝不**等于 ``%APPDATA%\\BambuMonitor``。
    该夹具是 autouse 的，所以任何测试都不可能误写用户真实配置。
    """
    target = tmp_path / "config"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BAMBU_MONITOR_CONFIG_DIR", str(target))

    from app import config

    resolved = Path(config.config_dir()).resolve()
    real = real_config_dir().resolve()
    assert resolved.is_dir(), f"隔离后的配置目录不存在：{resolved}"
    assert resolved != real, f"配置目录没有隔离，仍指向真实目录：{resolved}"
    assert real not in resolved.parents, f"配置目录落在真实目录里：{resolved}"
    return target


@pytest.fixture
def no_network(monkeypatch):
    """禁止一切真实网络活动（离线 + 不占端口的强制保证）。

    纯逻辑层测试不应产生任何 socket 连接或监听；一旦产生，
    会立刻以 ``RuntimeError`` 失败，而不是悄悄连上真实设备。

    故意**不设 autouse**：只由本目录里负责纯逻辑层的测试文件通过
    ``pytestmark = pytest.mark.usefixtures("no_network")`` 显式启用，
    以免影响其它测试文件（例如需要本地回环端口的 Web 服务测试）。
    """

    def _blocked(*args, **kwargs):
        raise RuntimeError("测试禁止联网 / 绑定端口（请保持纯逻辑层离线可重复）")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket.socket, "bind", _blocked)
    monkeypatch.setattr(socket.socket, "listen", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    return _blocked
