"""本机网卡枚举的测试（``app/bambu/discovery.py``）。

## 为什么单独立一条

原来的枚举链路只有「Windows 适配器表」和「主机名解析 + UDP connect 探测」两条。
后者**依赖外网连通**（探测 223.5.5.5 / 8.8.8.8 / 1.1.1.1）：纯内网环境里没有任何
默认路由，于是它什么也拿不到 —— 用户看到的是「一台设备都搜不到」，而且没有任何
补救手段。现在补了两条不依赖外网的路径：POSIX ``ioctl`` 与环境变量
``BAMBU_MONITOR_SUBNETS``。

本文件**不发送任何报文**：``_env_interfaces`` / ``_dedupe_interfaces`` 是纯计算，
``local_interfaces`` 用 monkeypatch 把三层来源全部替换掉。
"""

from __future__ import annotations

import socket

import pytest

from app.bambu import discovery

pytestmark = pytest.mark.usefixtures("no_network")


# --------------------------------------------------------------------------- 环境变量指定网段


def test_没有设置环境变量时不返回任何网卡(monkeypatch):
    monkeypatch.delenv("BAMBU_MONITOR_SUBNETS", raising=False)
    assert discovery._env_interfaces() == []


@pytest.mark.parametrize(
    ("raw", "ip", "prefix", "broadcast", "mask"),
    [
        ("192.168.1.0/24", "192.168.1.1", "192.168.1.0", "192.168.1.255", 24),
        ("192.168.1.", "192.168.1.1", "192.168.1.0", "192.168.1.255", 24),
        ("192.168.1.77", "192.168.1.1", "192.168.1.0", "192.168.1.255", 24),
        ("10.0.0.0/16", "10.0.0.1", "10.0.0.0", "10.0.255.255", 16),
    ],
)
def test_解析单个网段(monkeypatch, raw, ip, prefix, broadcast, mask):
    monkeypatch.setenv("BAMBU_MONITOR_SUBNETS", raw)
    interfaces = discovery._env_interfaces()
    assert len(interfaces) == 1
    iface = interfaces[0]
    assert (iface.ip, iface.prefix, iface.broadcast, iface.mask) == (ip, prefix, broadcast, mask)


def test_多个网段可以用逗号或空格分隔(monkeypatch):
    monkeypatch.setenv("BAMBU_MONITOR_SUBNETS", "192.168.1.0/24, 10.1.0.0/24 172.16.5.0/24")
    nets = {iface.prefix for iface in discovery._env_interfaces()}
    assert nets == {"192.168.1.0", "10.1.0.0", "172.16.5.0"}


def test_非法网段与越界掩码都不会让搜索崩掉(monkeypatch):
    """``不是网段`` 被跳过；``/33`` 这种越界掩码按 /24 处理（而不是夹到 /30）。"""
    monkeypatch.setenv("BAMBU_MONITOR_SUBNETS", "不是网段,192.168.9.0/33,10.2.0.0/24")
    interfaces = discovery._env_interfaces()
    assert [iface.prefix for iface in interfaces] == ["192.168.9.0", "10.2.0.0"]
    assert [iface.mask for iface in interfaces] == [24, 24]


def test_手工指定的网段一定可扫描():
    """契约：用户手工指定的网段不能被「虚拟网卡」过滤器吃掉。"""
    iface = discovery.LocalInterface(
        ip="192.168.31.1",
        prefix="192.168.31.0",
        broadcast="192.168.31.255",
        name="BAMBU_MONITOR_SUBNETS",
        mask=24,
    )
    assert iface.scannable is True
    assert iface.sweepable is True


# --------------------------------------------------------------------------- 去重与合并


def test_按ip去重且保留先出现的():
    """契约：越靠前的来源越可靠，重复 IP 保留先出现的那条（含它的掩码与名称）。"""
    first = discovery.LocalInterface(ip="192.168.1.5", prefix="192.168.1.0", broadcast="192.168.1.255", name="eth0", mask=24)
    second = discovery.LocalInterface(ip="192.168.1.5", prefix="192.168.1.0", broadcast="192.168.1.255", name="猜测来源", mask=16)
    result = discovery._dedupe_interfaces([first, second])
    assert result == [first]


def test_空的ip被丢掉():
    empty = discovery.LocalInterface(ip="", prefix="", broadcast="")
    assert discovery._dedupe_interfaces([empty]) == []


def test_local_interfaces_合并三层来源并优先使用系统网卡(monkeypatch):
    """契约：系统枚举结果优先，环境变量网段作为补充，并且不依赖外网就能给出结果。"""
    system = discovery.LocalInterface(
        ip="192.168.31.100", prefix="192.168.31.0", broadcast="192.168.31.255", name="以太网", mask=24
    )
    monkeypatch.setattr(discovery, "_adapter_interfaces", lambda: [system])
    monkeypatch.setattr(discovery, "_posix_interfaces", lambda: [])
    monkeypatch.setattr(
        discovery,
        "_resolve_interfaces",
        lambda: pytest.fail("系统已能枚举出网卡时不该再走依赖外网的兜底路径"),
    )
    monkeypatch.setenv("BAMBU_MONITOR_SUBNETS", "10.9.0.0/24")

    interfaces = discovery.local_interfaces()
    assert [iface.ip for iface in interfaces] == ["192.168.31.100", "10.9.0.1"]


def test_纯内网时用_ioctl_结果而不是外网探测(monkeypatch):
    """核心回归：没有外网（_adapter_interfaces 在 Linux 上返回空）也要枚举出网卡。"""
    posix = discovery.LocalInterface(
        ip="192.168.31.100", prefix="192.168.31.0", broadcast="192.168.31.255", name="eth0", mask=24
    )
    monkeypatch.setattr(discovery, "_adapter_interfaces", lambda: [])
    monkeypatch.setattr(discovery, "_posix_interfaces", lambda: [posix])
    monkeypatch.setattr(
        discovery,
        "_resolve_interfaces",
        lambda: pytest.fail("有 ioctl 结果时不该再走依赖外网的兜底路径"),
    )
    monkeypatch.delenv("BAMBU_MONITOR_SUBNETS", raising=False)

    interfaces = discovery.local_interfaces()
    assert [iface.ip for iface in interfaces] == ["192.168.31.100"]


def test_枚举不出网卡时打日志提示手工指定(monkeypatch, caplog):
    monkeypatch.setattr(discovery, "_adapter_interfaces", lambda: [])
    monkeypatch.setattr(discovery, "_posix_interfaces", lambda: [])
    monkeypatch.setattr(discovery, "_resolve_interfaces", lambda: [])
    monkeypatch.delenv("BAMBU_MONITOR_SUBNETS", raising=False)

    with caplog.at_level("WARNING", logger="bambu-monitor.discovery"):
        assert discovery.local_interfaces() == []
    assert any("BAMBU_MONITOR_SUBNETS" in record.message for record in caplog.records)


def test_posix枚举在缺少fcntl的平台上安全返回空(monkeypatch):
    """契约：Windows 上没有 ``fcntl``，这里必须返回空列表而不是 ImportError。

    （Windows 走 ``GetAdaptersAddresses``；真正跑在 Linux 上时本用例等价于
    「ioctl 失败也要安全返回」。）
    """
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("模拟没有 fcntl 的平台")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert discovery._posix_interfaces() == []


def test_兜底解析会过滤回环(monkeypatch):
    """``_resolve_interfaces`` 不能把 127.x 当网卡返回（否则会扫整个回环网段）。"""
    monkeypatch.setattr(socket, "gethostname", lambda: "test-host")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, 0, 0, "", ("127.0.1.1", 0))],
    )
    assert discovery._resolve_interfaces() == []
