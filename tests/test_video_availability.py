"""画面可达性的契约：没有 OpenCV 时也要能出画面（安卓版就是这种环境）。

## 背景（这一版修掉的真问题）

安卓版的 APK **刻意不打包 opencv/numpy**：Chaquopy 仓库里的这两个轮子是
**4096 字节对齐**的（已用 ELF 段头实测：``p_align=0x1000``），在 16KB 内存页设备上
会被系统拒绝加载 → 一导入就闪退。于是只提供 RTSPS(322) 通道的机型
（X1 / X1C / X2D / H2 / P2S）在安卓上**永远没有画面**，而用户只看到「连接中」。

现在的做法：**不在这边解码**。用纯 Python 把 H.264 码流取出来
（`app/bambu/rtsp_h264.py`：RTSP + Digest + TCP interleaved RTP + RFC 6184 解包），
交给网页端的 WebCodecs 解码 —— 那边本来就有硬件解码器。

本文件锁住三件事：

1. 没有 OpenCV 时**不再**退回注定失败的 6000，而是走 H.264 通路；
2. 只有真正无解（连 H.264 通路也没起来）时才给"看不到画面"的说明；
3. 网页端要的 `latest_h264()` 行为正确（取走即清空、有上限、没有码流时为空）。
"""

from __future__ import annotations

import pytest

from app.bambu.models import PrinterInfo, PrinterModel
from app.bambu.printer import H264_QUEUE_LIMIT, PrinterSession

pytestmark = pytest.mark.usefixtures("no_network")


def _session(model: PrinterModel, code: str = "12345678") -> PrinterSession:
    return PrinterSession(PrinterInfo(ip="127.0.0.1", model=model, access_code=code))


class _FakeParams:
    ready = True
    codec_string = "avc1.641029"
    avcc_description = b"\x01d\x10)\xfc"


class _FakeH264Client:
    """替身：不连网络，只记录被怎么调用。"""

    instances: list["_FakeH264Client"] = []

    def __init__(self, host, access_code, on_access_unit=None, on_state=None, name="", **kwargs):
        self.host = host
        self.access_code = access_code
        self.on_access_unit = on_access_unit
        self.on_state = on_state
        self.name = name
        self.state = "connecting"
        self.detail = "测试替身"
        self.parameters = _FakeParams()
        self.started = False
        self.stopped = False
        _FakeH264Client.instances.append(self)

    def start(self) -> None:  # noqa: D102
        self.started = True

    def stop(self) -> None:  # noqa: D102
        self.stopped = True

    def join(self, timeout=None) -> None:  # noqa: ANN001, D102
        pass

    def wait_first_unit(self, timeout=10.0) -> bool:  # noqa: ANN001, D102
        return True


@pytest.fixture()
def no_opencv(monkeypatch):
    """模拟安卓版：没有 cv2。"""
    import app.bambu.rtsp as rtsp

    monkeypatch.setattr(rtsp.RtspStream, "available", staticmethod(lambda: False))
    _FakeH264Client.instances.clear()
    monkeypatch.setattr("app.bambu.rtsp_h264.RtspH264Client", _FakeH264Client)
    return _FakeH264Client


# --------------------------------------------------------------------------- 通道选择


def test_没有opencv时只支持RTSPS的机型走H264通路(no_opencv):
    """核心回归：以前这里退回 6000（注定失败）→ 永远没画面；现在走纯 Python 通路。"""
    session = _session(PrinterModel.X2D)
    assert session._preferred_channel() == "rtsp", "应当选 RTSPS，而不是注定失败的 6000"

    session._start_h264()
    client = no_opencv.instances[-1]
    assert client.started is True
    assert session.video_mode == "h264"
    assert session.video_channel == "rtsp-h264"
    assert not session.video_unavailable_reason, "通路起来了就不该说「看不到画面」"


def test_有opencv时仍然用RtspStream(monkeypatch):
    """契约：桌面版/服务端有 OpenCV，继续走原来那条路（能直接拿 JPEG 帧）。"""
    import app.bambu.rtsp as rtsp

    monkeypatch.setattr(rtsp.RtspStream, "available", staticmethod(lambda: True))
    session = _session(PrinterModel.X2D)
    assert session._preferred_channel() == "rtsp"
    assert session.video_mode == "jpeg", "没有起 H.264 通路时默认还是 JPEG 模式"


def test_只走6000的机型不受影响(no_opencv):
    """A1 / P1 系列用 6000 端口 JPEG，与 OpenCV、H.264 都无关。"""
    for model in (PrinterModel.P1S, PrinterModel.A1, PrinterModel.A1MINI, PrinterModel.P1P):
        session = _session(model)
        assert session._preferred_channel() == "tcp6000"
        assert session.video_unavailable_reason == ""


# --------------------------------------------------------------------------- 说明文案


def test_通路没起来时才说看不到画面(no_opencv):
    session = _session(PrinterModel.X2D)
    reason = session.video_unavailable_reason
    assert reason, "既没有 OpenCV、H.264 通路也没起来时，必须说明原因"
    assert "RTSPS" in reason
    assert "遥测" in reason, "要安抚用户：其它功能不受影响"

    session._start_h264()
    assert session.video_unavailable_reason == "", "通路起来后不该再报看不到画面"


def test_没填访问代码时不报解码器问题():
    session = _session(PrinterModel.X2D, code="")
    assert session.video_unavailable_reason == ""


# --------------------------------------------------------------------------- 网页端要的数据


class _Unit:
    def __init__(self, index: int = 1) -> None:
        self.data = b"\x00\x00\x00\x01\x65"
        self.is_keyframe = True
        self.timestamp_us = index
        self.sequence = index


def test_latest_h264_取走即清空(no_opencv):
    session = _session(PrinterModel.X2D)
    session._start_h264()

    session._on_h264_unit(_Unit())
    params, units = session.latest_h264()
    assert params["codec"] == "avc1.641029"
    assert params["description"].startswith(b"\x01d")
    assert len(units) == 1
    _params, again = session.latest_h264()
    assert again == [], "取过一次就该清空（避免同一帧被反复推给网页）"


def test_h264_队列有上限不至于吃内存(no_opencv):
    session = _session(PrinterModel.X2D)
    session._start_h264()

    for index in range(H264_QUEUE_LIMIT + 10):
        session._on_h264_unit(_Unit(index))
    assert session.h264_queue_depth() == H264_QUEUE_LIMIT
    _params, units = session.latest_h264()
    assert [unit.sequence for unit in units] == list(
        range(10, H264_QUEUE_LIMIT + 10)
    ), "丢的应当是最旧的帧"


def test_没有H264通路时latest_h264为空(monkeypatch):
    import app.bambu.rtsp as rtsp

    monkeypatch.setattr(rtsp.RtspStream, "available", staticmethod(lambda: True))
    session = _session(PrinterModel.X2D)
    params, units = session.latest_h264()
    assert params == {} and units == []


def test_参数没就绪时不交出半成品(no_opencv):
    session = _session(PrinterModel.X2D)
    session._start_h264()
    client = no_opencv.instances[-1]
    client.parameters.ready = False
    params, units = session.latest_h264()
    assert params == {} and units == []
