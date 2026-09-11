"""设备族注册表的契约。

注册表是「新增一个品牌 = 注册一条数据」的落点。这里锁定三件事：

1. **向后兼容**：老配置里的 `PrinterInfo` 没有 `family` 字段，
   必须解析为拓竹族 —— 否则升级后老用户的所有设备都会失去归属；
2. **不臆造**：只登记已经能用的族（提前登记会让界面出现用不了的选项）；
3. **凭据策略可被界面消费**：标签/是否必填/是否隐藏都由数据决定，
   不再是写死的「访问代码」。
"""

from __future__ import annotations

import pytest

from app.bambu.models import PrinterInfo, PrinterModel
from app.core import registry


def test_拓竹族已登记且是默认族():
    """契约：拓竹族必须已登记，且 id 固定为 ``bambu``（配置兼容性的一部分）。"""
    descriptor = registry.get(registry.FAMILY_BAMBU)
    assert descriptor is not None
    assert descriptor.family == "bambu"
    assert descriptor.label
    assert descriptor.default_port == 8883


def test_老配置没有family字段时解析为拓竹():
    """契约（**向后兼容的关键**）：`PrinterInfo` 没有 family 字段时归入拓竹族。

    历史上写出的所有 config.json 都是这个形态；升级后它们必须继续可用，
    不需要任何迁移步骤。
    """
    legacy = PrinterInfo(ip="192.168.1.50", model=PrinterModel.P1S)
    assert not hasattr(legacy, "family") or legacy.family == ""
    resolved = registry.resolve_family(legacy)
    assert resolved.family == registry.FAMILY_BAMBU


def test_未知family值退回拓竹而不是崩掉():
    """契约：配置里出现本版本不认识的 family（例如来自更新的版本）时，
    退回拓竹族而不是让界面拿到 None 崩掉。"""

    class Weird:
        family = "some-future-family"

    assert registry.resolve_family(Weird()).family == registry.FAMILY_BAMBU


def test_只登记已经能用的设备族():
    """契约：注册表里不出现「已登记但用不了」的族。

    moonraker 已登记是因为它有了实现并测试过的适配器
    （`tests/test_moonraker.py`）；未落地的族（octoprint）不得出现在这里。
    """
    families = {descriptor.family for descriptor in registry.all_families()}
    assert registry.FAMILY_BAMBU in families
    assert registry.FAMILY_MOONRAKER in families
    assert registry.FAMILY_OCTOPRINT not in families, "适配器未落地就不该登记"
    for family in families:
        descriptor = registry.get(family)
        assert descriptor is not None
        assert descriptor.label, f"{family} 缺少展示名"
        assert descriptor.credential.label, f"{family} 缺少凭据标签"


def test_拓竹族的凭据策略是必填访问代码():
    """契约：拓竹需要 8 位访问代码，界面据此要求填写。"""
    policy = registry.get(registry.FAMILY_BAMBU).credential
    assert policy.key == "access_code", "配置字段名不能改（老配置依赖它）"
    assert policy.label == "访问代码"
    assert policy.required is True
    assert policy.secret is True
    assert "局域网" in policy.hint


def test_拓竹族的候选端口覆盖三个服务():
    """契约：诊断与手动添加要能拿到 8883 / 6000 / 322 三个端口。"""
    descriptor = registry.get(registry.FAMILY_BAMBU)
    assert set(descriptor.candidate_ports) == {8883, 6000, 322}


def test_描述符记录了发现方式与注意事项():
    """契约：发现方式与已知坑要随描述符一起带上，供界面提示与诊断展示。

    「新机型需 Developer Mode」这条来自真机实测，写进描述符是为了让
    界面/诊断能直接引用，而不是散落在各处注释里。
    """
    descriptor = registry.get(registry.FAMILY_BAMBU)
    assert descriptor.discoveries, "应记录发现方式"
    assert any("SSDP" in item for item in descriptor.discoveries)
    assert "Developer Mode" in descriptor.notes


def test_登记与查询是一致的():
    """契约：`register` / `get` / `is_registered` 三者必须一致。"""
    custom = registry.FamilyDescriptor(
        family="unit-test-family",
        label="单元测试族",
        credential=registry.CredentialPolicy(label="API Key", required=False, secret=True),
    )
    try:
        registry.register(custom)
        assert registry.is_registered("unit-test-family")
        assert registry.get("unit-test-family") is custom
        assert custom in registry.all_families()
    finally:
        # 清理，避免污染其它测试
        registry._REGISTRY.pop("unit-test-family", None)
    assert not registry.is_registered("unit-test-family")


def test_注册表按id排序保证展示顺序稳定():
    """契约：`all_families()` 顺序稳定（界面按它渲染，顺序跳动会让人困惑）。"""
    names = [descriptor.family for descriptor in registry.all_families()]
    assert names == sorted(names)


@pytest.mark.parametrize("family", [registry.FAMILY_OCTOPRINT])
def test_未落地生态的id已预留但未登记(family):
    """契约：OctoPrint 的 id 已定义为常量，但**尚未登记**（适配器还没写）。

    常量先定义是为了让适配器实现时不用改公共接口；未登记是刻意的
    —— 提前登记会让界面出现一个选了也没用的选项。
    """
    assert not registry.is_registered(family)
    assert family


def test_moonraker族已登记且凭据不是必填():
    """契约：Moonraker 族已登记（适配器已落地），且 **API Key 不是必填**。

    内网默认免鉴权（Moonraker 的 trusted_clients 含各私网段），强制要求填
    API Key 会把最常见的场景挡在门外。
    """
    descriptor = registry.get(registry.FAMILY_MOONRAKER)
    assert descriptor is not None
    policy = descriptor.credential
    assert policy.key == "api_key"
    assert policy.required is False, "内网免鉴权场景不该强制填 API Key"
    assert policy.secret is True


def test_moonraker候选端口是80优先7125回退():
    """契约：候选端口顺序为 80 → 7125。

    Snapmaker U1 出厂配置前挂了 nginx，80 端口即可访问 Moonraker API
    （官方端口表 + 真机探测都确认）；通用 Klipper 机器则是 7125。
    顺序反了会让 U1 用户白等一轮超时。
    """
    descriptor = registry.get(registry.FAMILY_MOONRAKER)
    assert descriptor.candidate_ports[0] == 80
    assert 7125 in descriptor.candidate_ports


def test_moonraker描述符记录了WS_only这条坑():
    """契约：必须写明「急停与 control/* 是 WebSocket-only」这条实现约束。

    不写下来，后来者很可能按 REST 直觉去实现，结果是"点急停没反应"。
    U1 的主发现通道 `_snapmaker._tcp` 也要记录（它不是 `_moonraker._tcp`）。
    """
    descriptor = registry.get(registry.FAMILY_MOONRAKER)
    assert "WebSocket" in descriptor.notes
    assert any("snapmaker" in item.lower() for item in descriptor.discoveries), (
        "U1 的主发现通道是 _snapmaker._tcp，必须记录"
    )
