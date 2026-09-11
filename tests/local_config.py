"""测试用的配置隔离工具。

``app.config`` 的配置目录由环境变量 ``BAMBU_MONITOR_CONFIG_DIR`` 决定，默认是
``%APPDATA%\\BambuMonitor``。**测试绝对不应该读写用户真实的打印机配置**
（那里面存着访问代码，而且测试产生的垃圾配置会污染用户的实际使用）。

两种用法：

1. 需要自己控制目录时，用 ``isolated_config_dir`` 夹具（基于 ``tmp_path``）：:

       def test_something(isolated_config_dir):
           from app.config import AppConfig
           AppConfig().save()          # 写进临时目录

2. 不关心目录、只要不碰用户配置时，直接依赖 ``no_real_config`` 夹具：:

       def test_something(no_real_config):
           ...

另外 ``import app.*`` 之前请不要让模块级代码读取配置；``AppConfig.load()`` 只在
显式调用时才读盘，所以通常无需担心。
"""

from __future__ import annotations

import os
import tempfile

import pytest

#: 测试期间使用的配置目录名（放在系统临时目录下，避免污染仓库）
_SENTINEL = "BAMBU_MONITOR_TEST_CONFIG_DIR"


@pytest.fixture()
def isolated_config_dir(monkeypatch, tmp_path):
    """把配置目录指向本次测试专属的临时目录。"""
    target = tmp_path / "bambu-config"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BAMBU_MONITOR_CONFIG_DIR", str(target))
    return target


@pytest.fixture()
def no_real_config(monkeypatch):
    """只为隔离——把配置目录指到一个进程级临时目录，不关心具体路径。"""
    target = os.path.join(tempfile.gettempdir(), "bambu-monitor-tests", _SENTINEL)
    os.makedirs(target, exist_ok=True)
    monkeypatch.setenv("BAMBU_MONITOR_CONFIG_DIR", target)
    return target
