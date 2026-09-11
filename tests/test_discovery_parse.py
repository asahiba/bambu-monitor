"""局域网发现的两条报文解析链路的纯逻辑测试（``app/bambu/discovery.py``）。

只测**纯解析**（``parse_announcement`` / ``_looks_like_bambu`` / ``merge_devices``
与 ``LocalInterface`` 的判定属性），**绝不**发送任何 UDP 组播/广播，也不会碰局域网：

* SSDP（新固件，1990 端口）响应与 ``ssdp:alive`` 通知
* 2021 端口旧固件的 JSON 广播
* 畸形输入（空串、非 JSON、缺字段、超长字段、非法 UTF-8）不得抛异常
* 非拓竹设备（路由器/NAS 的 UPnP 回包）必须被过滤掉
* ``merge_devices`` 的去重与字段补齐

报文样例取自 ``docs/PROTOCOL.md``。
"""

from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest

from app.bambu.discovery import (
    LocalInterface,
    _looks_like_bambu,
    _prefix_of,
    merge_devices,
    parse_announcement,
)
from app.bambu.models import PrinterInfo, PrinterModel

#: docs/PROTOCOL.md §1.1 的 SSDP 单播回包
SSDP_REPLY = (
    "HTTP/1.1 200 OK\r\n"
    "Location: 192.168.1.50\r\n"
    "USN: 01P00A1234567\r\n"
    "Server: UPnP/1.0\r\n"
    "devmodel.bambu.com: P1S\r\n"
    "devname.bambu.com: 3DP-001\r\n"
    "devconnect.bambu.com: lan\r\n"
    "devbind.bambu.com: free\r\n"
    "devversion.bambu.com: 01.05.00.00\r\n"
    "\r\n"
).encode("utf-8")

#: docs/PROTOCOL.md §1.2 的 2021 端口 JSON
LEGACY_JSON = json.dumps(
    {
        "dev_ip": "192.168.1.50",
        "dev_id": "01P00A1234567",
        "dev_name": "3DP-001",
        "dev_version": "01.05.00.00",
        "dev_signal": "-50",
        "dev_connect": "lan",
    }
).encode("utf-8")


# ------------------------------------------------------------------ SSDP 解析
def test_解析SSDP响应():
    """契约：SSDP 回包解析出 IP / 序列号 / 机型 / 名称 / 固件，并标记为「搜索发现」。"""
    info = parse_announcement(SSDP_REPLY, "127.0.0.1")
    assert info is not None
    assert isinstance(info, PrinterInfo)
    assert info.ip == "192.168.1.50"
    assert info.serial == "01P00A1234567"
    assert info.name == "3DP-001"
    assert info.firmware == "01.05.00.00"
    assert info.model is PrinterModel.P1S
    assert info.discovered is True


def test_解析SSDP响应时头部大小写与空格不敏感():
    """契约：SSDP 头部名大小写不敏感、值两侧空格会被去掉。"""
    payload = (
        "HTTP/1.1 200 OK\r\n"
        "location:   192.168.1.51  \r\n"
        "usn: 01P00A1234567\r\n"
        "DevModel.Bambu.Com: P1S\r\n"
        "DEVNAME.BAMBU.COM: 车间一号\r\n"
        "DevVersion.Bambu.Com: 01.06.00.00\r\n"
        "\r\n"
    ).encode("utf-8")
    info = parse_announcement(payload)
    assert info is not None
    assert info.ip == "192.168.1.51"
    assert info.name == "车间一号"
    assert info.firmware == "01.06.00.00"
    assert info.model is PrinterModel.P1S


def test_SSDP缺少Location时用来源地址():
    """契约：回包里没有 Location 时，用 UDP 来源 IP 作为设备地址。"""
    payload = (
        "HTTP/1.1 200 OK\r\n"
        "USN: 01P00A1234567\r\n"
        "devmodel.bambu.com: P1S\r\n"
        "\r\n"
    ).encode("utf-8")
    info = parse_announcement(payload, "192.168.1.77")
    assert info is not None
    assert info.ip == "192.168.1.77"
    assert info.serial == "01P00A1234567"


def test_解析SSDP通知报文():
    """契约：被动的 ssdp:alive 通知（首行是 NOTIFY）与 M-SEARCH 回包一样能解析。"""
    payload = (
        "NOTIFY * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1990\r\n"
        "NT: urn:bambulab-com:device:3dprinter:1\r\n"
        "USN: 20P6BJ632400723\r\n"
        "Location: 192.168.1.60\r\n"
        "\r\n"
    ).encode("utf-8")
    info = parse_announcement(payload)
    assert info is not None
    assert info.serial == "20P6BJ632400723"
    assert info.ip == "192.168.1.60"
    assert info.model is PrinterModel.X2D, "20P 前缀应识别为 X2D"


def test_只有拓竹序列号也能认出来():
    """契约：没有任何 dev* 字段时，15 位大写字母数字的 USN 也按拓竹设备接受。"""
    payload = (
        "HTTP/1.1 200 OK\r\n"
        "Location: 192.168.1.61\r\n"
        "USN: 03900A234567890\r\n"
        "\r\n"
    ).encode("utf-8")
    info = parse_announcement(payload)
    assert info is not None
    assert info.serial == "03900A234567890"
    assert info.model is PrinterModel.A1
    assert info.name == ""


def test_非拓竹设备的SSDP回包被忽略():
    """契约：路由器/NAS 等普通 UPnP 设备的回包必须被过滤，不能出现在搜索结果里。"""
    router = (
        "HTTP/1.1 200 OK\r\n"
        "Location: http://192.168.1.1:1900/rootDesc.xml\r\n"
        "USN: uuid:4d696c6e-6e65-7373-0000-000000000000::urn:schemas-upnp-org:device\r\n"
        "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
        "\r\n"
    ).encode("utf-8")
    assert parse_announcement(router, "192.168.1.1") is None


def test_自己发出的M_SEARCH被忽略():
    """契约：本机监听端口会收到自己发出的 M-SEARCH，必须丢弃。"""
    search = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1990\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 1\r\n"
        "ST: urn:bambulab-com:device:3dprinter:1\r\n"
        "\r\n"
    ).encode("ascii")
    assert parse_announcement(search, "192.168.1.100") is None


@pytest.mark.parametrize(
    ("headers", "serial", "model_name", "name", "expected"),
    [
        ({"devmodel.bambu.com": "P1S"}, "X", "P1S", "", True),
        ({"devname.bambu.com": "3DP-001"}, "X", "", "3DP-001", True),
        ({"devversion.bambu.com": "01.05.00.00"}, "X", "", "", True),
        ({"nt": "urn:bambulab-com:device:3dprinter:1"}, "", "", "", True),
        # M-SEARCH 的应答用 ST（设备主动通知才用 NT），两者都要能识别
        ({"st": "urn:bambulab-com:device:3dprinter:1"}, "01P00A123456789", "", "", True),
        # 只看 ST、且序列号不合法 —— 修复前这里会因为不检查 st 而被漏掉
        ({"st": "urn:bambulab-com:device:3dprinter:1"}, "short", "", "", True),
        ({"st": "urn:bambulab-com:device:3dprinter:1"}, "not-a-serial", "", "", True),
        ({"st": "urn:schemas-upnp-org:device:MediaRenderer:1"}, "short", "", "", False),
        ({}, "01p00a123456789", "", "", False),  # 小写序列号：不像拓竹
        ({}, "01P00A12345678", "", "", False),  # 14 位：长度不对
        ({}, "", "", "", False),
        ({}, "uuid:1234", "", "", False),
    ],
)
def test_拓竹设备识别规则(headers, serial, model_name, name, expected):
    """契约：优先看拓竹专有字段、NT/ST 通知类型，其次要求 15 位大写字母数字序列号。"""
    assert _looks_like_bambu(dict(headers), serial, model_name, name) is expected


# ------------------------------------------------------------------ 2021 JSON
def test_解析2021端口JSON广播():
    """契约：旧固件的 JSON 广播解析出 IP / 序列号 / 名称 / 固件。"""
    info = parse_announcement(LEGACY_JSON, "127.0.0.1")
    assert info is not None
    assert info.ip == "192.168.1.50"
    assert info.serial == "01P00A1234567"
    assert info.name == "3DP-001"
    assert info.firmware == "01.05.00.00"
    assert info.model is PrinterModel.P1S
    assert info.discovered is True


def test_2021广播缺dev_ip时用来源地址():
    """契约：JSON 里没有 dev_ip 时回落到 UDP 来源 IP。"""
    payload = json.dumps({"dev_id": "01P00A1234567", "dev_name": "3DP-001"}).encode()
    info = parse_announcement(payload, "192.168.1.88")
    assert info is not None
    assert info.ip == "192.168.1.88"
    assert info.serial == "01P00A1234567"


def test_2021广播字段类型异常时安全转换():
    """契约：JSON 字段是数字/None 时转成字符串，不能抛异常。"""
    payload = json.dumps(
        {"dev_ip": "192.168.1.50", "dev_id": 123456789012345, "dev_name": None, "dev_version": 1}
    ).encode()
    info = parse_announcement(payload)
    assert info is not None
    assert info.serial == "123456789012345"
    assert info.name == ""
    assert info.firmware == "1"


@pytest.mark.parametrize(
    "payload",
    [
        "{}",  # 空对象：既没 IP 也没序列号
        '{"dev_id": ""}',
        '{"dev_ip": "192.168.1.50"}',  # 只有 IP，没有序列号也没有名字
        '{"dev_name": ""}',
        "[]",  # JSON 数组不是对象
        '"just a string"',
    ],
)
def test_2021广播缺字段时返回None(payload):
    """契约：关键字段缺失时返回 None（视为无效广播），不得抛异常。"""
    assert parse_announcement(payload.encode(), "192.168.1.9") is None


# ------------------------------------------------------------------ 畸形输入
@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"   \r\n\r\n",
        b"{not json",
        b'{"dev_ip": "1.2.3.4", "dev_id":}',  # JSON 截断
        b"\x00\x01\x02\x03",
        b"\xff\xfe\xfd",  # 非法 UTF-8
        b"HTTP/1.1 200 OK\r\n\r\n",  # 只有状态行
        b"HTTP/1.1 200 OK\r\nno-colon-line\r\n\r\n",
        b":\r\n",  # 空头部名
        b"HTTP/1.1 200 OK\r\nLocation:\r\nUSN:\r\n\r\n",
        b"M99999",
        b"NOTIFY * HTTP/1.1\r\n\r\n",
    ],
)
def test_畸形输入不抛异常(payload):
    """契约：任何畸形报文都只能返回 None，绝不能抛异常打断搜索线程。"""
    result = parse_announcement(payload, "192.168.1.9")
    assert result is None or isinstance(result, PrinterInfo)


def test_超长字段不抛异常():
    """契约：超长字段（恶意/异常设备）也必须安全处理。"""
    long_value = "A" * 200_000
    payload = (
        f"HTTP/1.1 200 OK\r\nLocation: 192.168.1.50\r\nUSN: {long_value}\r\n"
        "devmodel.bambu.com: P1S\r\n\r\n"
    ).encode()
    info = parse_announcement(payload)
    assert info is not None
    assert info.ip == "192.168.1.50"
    assert info.model is PrinterModel.P1S


def test_超长JSON不抛异常():
    """契约：超大 JSON 广播（例如被截断/污染）不得抛异常。"""
    payload = ('{"dev_ip": "192.168.1.50", "dev_name": "' + "长" * 100_000 + '"}').encode()
    info = parse_announcement(payload)
    assert info is not None and info.ip == "192.168.1.50"

    truncated = ('{"dev_ip": "192.168.1.50", "dev_name": "' + "长" * 100_000).encode()
    assert parse_announcement(truncated) is None


# ------------------------------------------------------------------ 合并结果
def test_merge_devices_新增设备():
    """契约：搜索结果里没见过的设备按顺序追加到列表末尾。"""
    existing = [PrinterInfo(ip="192.168.1.10", serial="01P00A1234567", name="老机器")]
    found = [PrinterInfo(ip="192.168.1.11", serial="03900A2345678", name="新机器")]
    merged = merge_devices(existing, found)
    assert [item.serial for item in merged] == ["01P00A1234567", "03900A2345678"]


def test_merge_devices_按序列号去重并补齐字段():
    """契约：同一台设备（序列号相同）只保留一条，并把缺失的 IP/名称/固件/机型补齐。"""
    existing = [PrinterInfo(ip="192.168.1.10", serial="01P00A1234567")]
    found = [
        PrinterInfo(
            ip="192.168.1.10",
            serial="01P00A1234567",
            name="车间 P1S",
            firmware="01.05.00.00",
            model=PrinterModel.P1S,
        )
    ]
    merged = merge_devices(existing, found)
    assert len(merged) == 1
    assert merged[0] is existing[0], "应就地补全已存在的对象"
    assert merged[0].name == "车间 P1S"
    assert merged[0].firmware == "01.05.00.00"
    assert merged[0].model is PrinterModel.P1S


def test_merge_devices_不覆盖已有字段():
    """契约：已有字段（用户可能改过）不被搜索结果覆盖。"""
    existing = [
        PrinterInfo(ip="192.168.1.10", serial="01P00A1234567", name="我的名字", firmware="00.00.1")
    ]
    found = [
        PrinterInfo(
            ip="192.168.1.99",
            serial="01P00A1234567",
            name="广播名字",
            firmware="99.99.99",
            model=PrinterModel.X1C,
        )
    ]
    merged = merge_devices(existing, found)
    assert len(merged) == 1
    assert merged[0].ip == "192.168.1.10"
    assert merged[0].name == "我的名字"
    assert merged[0].firmware == "00.00.1"


def test_merge_devices_未知机型被更具体机型覆盖():
    """契约：机型从「未知机型」升级为识别出的机型。"""
    existing = [PrinterInfo(ip="192.168.1.10", serial="01P00A1234567")]
    assert existing[0].model is PrinterModel.UNKNOWN
    found = [PrinterInfo(ip="192.168.1.10", serial="01P00A1234567", model=PrinterModel.P1S)]
    merged = merge_devices(existing, found)
    assert merged[0].model is PrinterModel.P1S


def test_merge_devices_无序列号时按IP去重():
    """契约：没有序列号时用 IP 作为去重键。"""
    existing = [PrinterInfo(ip="192.168.1.10", name="甲")]
    found = [PrinterInfo(ip="192.168.1.10", name="乙"), PrinterInfo(ip="192.168.1.11", name="丙")]
    merged = merge_devices(existing, found)
    assert len(merged) == 2
    assert [item.name for item in merged] == ["甲", "丙"]


def test_merge_devices_空输入():
    """契约：空输入不报错，返回列表。"""
    assert merge_devices([], []) == []
    only_new = merge_devices([], [PrinterInfo(ip="192.168.1.10", serial="01P00A1234567")])
    assert len(only_new) == 1


# ------------------------------------------------------------------ 网卡判定
@pytest.mark.parametrize(
    ("ip", "scannable"),
    [
        ("192.168.1.10", True),
        ("10.0.0.5", True),
        ("169.254.3.4", False),  # 链路本地
        ("198.18.0.1", False),  # 代理/测试网段
        ("198.19.0.1", False),
    ],
)
def test_网卡是否值得扫描(ip, scannable):
    """契约：链路本地与代理网段不做网段扫描。"""
    iface = LocalInterface(ip=ip, prefix=_prefix_of(ip), broadcast=_prefix_of(ip) + ".255")
    assert iface.scannable is scannable


@pytest.mark.parametrize(
    ("name", "mask", "sweepable"),
    [
        ("Intel(R) Wi-Fi 6 AX201 160MHz", 24, True),
        ("Realtek PCIe GbE Family Controller", 24, True),
        ("VMware Network Adapter VMnet8", 24, False),
        ("vEthernet (WSL)", 24, False),
        ("Tailscale", 24, False),
        ("Intel(R) Wi-Fi 6 AX201 160MHz", 16, False),  # 掩码太短，网段太大
    ],
)
def test_虚拟网卡不做网段单播扫描(name, mask, sweepable):
    """契约：虚拟/代理网卡与短掩码网段不做单播扫描（避免空扫与污染局域网）。"""
    iface = LocalInterface(
        ip="192.168.1.10", prefix="192.168.1", broadcast="192.168.1.255", name=name, mask=mask
    )
    assert iface.sweepable is sweepable


def test_网段前缀提取():
    """契约：_prefix_of 取前三段作为网段；非法输入原样返回。"""
    assert _prefix_of("192.168.1.50") == "192.168.1"
    assert _prefix_of("10.0.0.1") == "10.0.0"
    assert _prefix_of("bad") == "bad"
    assert _prefix_of("") == ""
