"""设备无关内核的契约：能力描述与会话协议。

这一层是为了「接入第三方设备族时界面不用改」而建立的。因此这里重点锁定三件事：

1. 现有拓竹会话**满足** `DeviceSession` 协议（不满足就意味着界面某处仍在依赖细节）；
2. 机型能回答「有没有舱灯 / 腔温 / 摄像头」等能力问题，且取值与官方规格一致；
3. 界面与网页的渲染分支**按能力**而不是按机型（用假会话即可验证，
   不需要真实设备）。
"""

from __future__ import annotations

import pytest

from app.bambu.models import PrinterInfo, PrinterModel
from app.bambu.printer import PrinterSession
from app.core import DeviceCapabilities, DeviceSession


def test_拓竹会话满足设备会话协议():
    """契约：`PrinterSession` 必须满足 `DeviceSession` 协议。

    这是「界面与网页对具体实现无感」的前提。若这条失败，说明会话少了某个
    界面/网页会调用的成员，新设备族适配器也就无法替换进来。
    """
    session = PrinterSession(PrinterInfo(ip="127.0.0.1", model=PrinterModel.P1S))
    assert isinstance(session, DeviceSession), (
        "PrinterSession 不再满足 DeviceSession 协议："
        f"缺少 {sorted(set(DeviceSession.__protocol_attrs__) - set(dir(session)))}"
        if hasattr(DeviceSession, "__protocol_attrs__")
        else "PrinterSession 不再满足 DeviceSession 协议"
    )


def test_会话暴露能力且与机型一致():
    """契约：`session.capabilities` 与 `model.capabilities` 一致（未叠加运行时观测时）。"""
    session = PrinterSession(PrinterInfo(ip="127.0.0.1", model=PrinterModel.P1S))
    assert session.capabilities == PrinterModel.P1S.capabilities


def test_双喷嘴机型在上报第二路温度后能力变为2():
    """契约：`nozzle_count` 要叠加运行时观测——只有真的上报了第二路温度才算双喷嘴。"""
    session = PrinterSession(PrinterInfo(ip="127.0.0.1", model=PrinterModel.H2D))
    assert session.capabilities.nozzle_count == 2, "H2D 固有能力就是双喷嘴"

    single = PrinterSession(PrinterInfo(ip="127.0.0.1", model=PrinterModel.P1S))
    assert single.capabilities.nozzle_count == 1
    single.status.apply_report({"print": {"nozzle_temper_2": 210.0}})
    assert single.capabilities.nozzle_count == 2, "收到第二路温度后应升为 2"


@pytest.mark.parametrize(
    "model, has_light",
    [
        # 开放机型：按规格推断没有舱灯
        (PrinterModel.A1, False),
        (PrinterModel.A1MINI, False),
        # ⚠️ A2L 例外：开放式机型，但**实测有 chamber_light**，
        # 且用官方 MQTT 报文开关灯被回读确认成功（见下一个用例）
        (PrinterModel.A2L, True),
        # 封闭腔体机型：有舱灯
        (PrinterModel.P1P, True),
        (PrinterModel.P1S, True),
        (PrinterModel.P2S, True),
        (PrinterModel.X1C, True),
        (PrinterModel.X2D, True),
        (PrinterModel.H2D, True),
        (PrinterModel.H2C, True),
    ],
)
def test_舱灯能力按机型区分(model, has_light):
    """契约：按机型推断的舱灯能力（**仅供参考**，界面不应据此藏按钮）。

    注意 A2L 为 True —— 它是开放式机型，我原先按规格推断"无舱灯"，
    但真机上报了 `chamber_light`，导致灯按钮被藏、用户无法开关灯。
    详见下一个用例：真正的判据是「设备是否上报了灯」。
    """
    assert model.capabilities.can_control_light is has_light


def test_A2L有舱灯因为是实测的():
    """契约：A2L 必须声明有舱灯。

    实测（2026-09，固件 01.01.05.00）：
    `lights_report = [{"node": "chamber_light", "mode": "off"}]`，
    且用官方 MQTT `system/ledctrl` 报文开关灯都被回读确认成功。
    机型规格（"open-frame"）推不出灯光能力，只有设备上报才算数。
    """
    assert PrinterModel.A2L.has_enclosure_light is True
    assert PrinterModel.A2L.capabilities.can_control_light is True


def test_会话采纳设备实际上报的灯能力():
    """契约：只要设备上报了 `lights_report`，会话能力就允许控制灯。

    这是根本修法：不再靠机型猜测。哪怕某机型的规格推断是"无灯"，
    只要它真的上报了灯节点，界面就必须给出开关。
    """
    session = PrinterSession(PrinterInfo(ip="127.0.0.1", model=PrinterModel.A2L))
    # A1 是我们认为"没有灯"的机型，用它验证"上报即允许"这条兜底逻辑
    a1 = PrinterSession(PrinterInfo(ip="127.0.0.1", model=PrinterModel.A1))
    assert a1.capabilities.can_control_light is False, "A1 机型能力确实是没有灯"

    a1.status.apply_report({"print": {"lights_report": [{"node": "chamber_light", "mode": "on"}]}})
    assert a1.capabilities.can_control_light is True, "设备上报了灯就必须允许控制"
    assert a1.status.light_on is True

    session.status.apply_report({"print": {"lights_report": [{"node": "chamber_light", "mode": "off"}]}})
    assert session.capabilities.can_control_light is True
    assert session.status.light_on is False


@pytest.mark.parametrize(
    "model, has_chamber",
    [
        (PrinterModel.A1, False),
        (PrinterModel.A2L, False),
        (PrinterModel.P1S, False),
        (PrinterModel.X1C, True),
        (PrinterModel.X2D, True),
        (PrinterModel.H2C, True),
        (PrinterModel.H2D_PRO, True),
    ],
)
def test_腔温能力与机型属性一致(model, has_chamber):
    """契约：能力层必须与既有的机型属性保持一致（避免两套真相）。"""
    assert model.capabilities.has_chamber_sensor is has_chamber
    assert model.capabilities.has_chamber_sensor is model.has_chamber_sensor


def test_能力是不可变值对象():
    """契约：能力集不可变，可安全地跨线程共享与比较。"""
    caps = PrinterModel.P1S.capabilities
    with pytest.raises(Exception):
        caps.has_chamber_sensor = True  # type: ignore[misc]
    assert caps.merged(has_chamber_sensor=True).has_chamber_sensor is True
    assert caps.has_chamber_sensor is False, "派生不应改动原对象"


def test_webkit_only_rtsp_机型可被识别():
    """契约：只能走 RTSPS 的机型要能被识别出来（这类机型退回 6000 必然失败）。"""
    assert PrinterModel.X2D.capabilities.is_rtsp_only is True
    assert PrinterModel.P1S.capabilities.is_rtsp_only is False
    assert PrinterModel.A2L.capabilities.is_rtsp_only is False
    assert PrinterModel.X2D.capabilities.video_channel == "rtsp"
    assert PrinterModel.A2L.capabilities.video_channel == "tcp6000"


def test_能力默认值对第三方设备族友好():
    """契约：`DeviceCapabilities` 的默认值要能描述一台「纯遥测、无摄像头」的设备。

    第三方设备族（例如只有静态快照的 PrusaLink、或没有摄像头的 Klipper 机器）
    需要能表达「没有摄像头」，否则界面会一直尝试画画面。
    """
    minimal = DeviceCapabilities(has_camera=False, video_channel="none")
    assert minimal.has_camera is False
    assert minimal.has_chamber_sensor is False
    assert minimal.is_rtsp_only is False
