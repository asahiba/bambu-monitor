"""`PrinterSession.video_unavailable_reason` 的契约：出不了画面时必须说清原因。

## 为什么单独立一条

安卓版的 APK **刻意不打包 opencv/numpy**（Chaquopy 的预编译包是 4096 字节对齐，
在 16KB 内存页设备上会直接闪退，见 `docs/PACKAGING.md`）。代价是：
**只提供 RTSPS(322) 通道的机型**（X1 / X1C / X2D / H2 / P2S…）在安卓上永远没有画面。

以前的提示是「RTSPS 错误：未开启局域网实时画面，或访问代码不正确」——
把用户引向完全错误的方向（他明明开了、代码也是对的），而且**触屏看不到 tooltip**，
用户只看到一个永远空着的画面。现在：
`/api/printers` 会带上 `video_unavailable_reason`，网页把它**直接画在画面上**。
"""

from __future__ import annotations

import pytest

from app.bambu.models import PrinterInfo, PrinterModel
from app.bambu.printer import PrinterSession

pytestmark = pytest.mark.usefixtures("no_network")


def _session(model: PrinterModel, code: str = "12345678") -> PrinterSession:
    return PrinterSession(PrinterInfo(ip="127.0.0.1", model=model, access_code=code))


def test_没有_opencv_时只支持_RTSPS_的机型给出明确原因(monkeypatch):
    """核心情形：X2D（只有 322 通道）+ 本机没有 cv2 = 安卓版的真实处境。"""
    import app.bambu.rtsp as rtsp

    monkeypatch.setattr(rtsp.RtspStream, "available", staticmethod(lambda: False))
    reason = _session(PrinterModel.X2D).video_unavailable_reason

    assert reason, "应当给出原因，而不是让用户面对一个永远空着的画面"
    assert "RTSPS" in reason, "要说清是哪条通道"
    assert "OpenCV" in reason, "要说清是缺解码器"
    assert "16KB" in reason or "16 KB" in reason, "要说明为什么安卓版没内置它"
    assert "遥测" in reason, "要安抚用户：其它功能不受影响"
    assert "桌面版" in reason or "服务端" in reason, "要给出可行的出路"


@pytest.mark.parametrize(
    "model",
    [
        PrinterModel.P1S,
        PrinterModel.A1,
        PrinterModel.A1MINI,
        PrinterModel.P1P,
    ],
)
def test_走6000端口的机型不受影响(model, monkeypatch):
    """A1 / P1 系列走 6000 端口 JPEG，不依赖 OpenCV，不该报"看不到画面"。"""
    import app.bambu.rtsp as rtsp

    monkeypatch.setattr(rtsp.RtspStream, "available", staticmethod(lambda: False))
    assert _session(model).video_unavailable_reason == ""


def test_有_opencv_时不报(monkeypatch):
    """本机装了 OpenCV（桌面版 / Docker）就没有这个问题。"""
    import app.bambu.rtsp as rtsp

    monkeypatch.setattr(rtsp.RtspStream, "available", staticmethod(lambda: True))
    assert _session(PrinterModel.X2D).video_unavailable_reason == ""


def test_没有访问代码时先提示访问代码而不是解码器():
    """没填访问代码时，更该提示的是「去填代码」——不要用解码器问题盖住它。"""
    assert _session(PrinterModel.X2D, code="").video_unavailable_reason == ""
