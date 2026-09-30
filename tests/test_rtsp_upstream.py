"""RTSPS(322) 上游取流的策略约束（都来自 2026-09-28 的真机实测）。

## 这份文件盯的是三件事

1. **一台打印机同时只伺候一个画面客户端**。同一台 X1C：我们的程序连着它时，
   另一个客户端 40 秒只拿到 13 帧；把程序里的会话全断开，同一个客户端 20 秒拿到
   300 帧、OpenCV 那条路 29~30 fps（911 帧 / 31.2 秒，零卡顿）。所以：

   * 开流前只许做 **TCP 探测**（`port_listening`），不许再发 DESCRIBE 建会话；
   * 连续失败时退避必须**有耐心**（不能 3 秒一次地捅）。

2. **"帧率设成 4" 不该把 30 fps 的画面真的变成 4 fps**：旧代码是"先按 max_fps 节流
   再 read()"，于是用户调低每路帧率 = 连画面一起变幻灯片。现在每帧都解码
   （统计才准），只在交付侧按帧率与"有没有人看"决定编不编码。

3. **没人看的时候不要白烧 CPU**（720p 解码约 0.3 个核、再逐帧编码约 0.35 个核）。
"""

from __future__ import annotations

import time

import pytest

from app.bambu import rtsp
from app.bambu.rtsp import RtspStream, port_listening
from app.bambu.timeouts import (
    RTSP_RETRY_PAUSE,
    RTSPS_BACKOFF_FACTOR,
    RTSPS_BACKOFF_MAX,
    RTSPS_DELIVERY_IDLE,
    RTSPS_HEALTHY_SECONDS,
)

pytestmark = pytest.mark.usefixtures("no_network")


def _stream(**kwargs) -> RtspStream:
    return RtspStream(host="192.168.31.27", access_code="12345678", **kwargs)


# --------------------------------------------------------------------------- 端口探测
def test_端口探测不会把异常抛给调用方(monkeypatch):
    """契约：探测失败就是"连不上"。离线测试环境里 socket 会直接抛别的异常，
    探测函数必须自己兜住 —— 选通道的代码不该因为一次探测失败就崩。"""

    def boom(*args, **kwargs):
        raise RuntimeError("测试禁止联网")

    monkeypatch.setattr(rtsp.socket, "create_connection", boom)
    assert port_listening("192.168.31.27") is False


def test_端口探测成功时不建RTSP会话(monkeypatch):
    """契约：只连 TCP，连上就关 —— 不发任何 RTSP 请求（会话会占住画面通道）。"""
    calls: list[tuple] = []

    class FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def sendall(self, data):  # pragma: no cover - 不该被调用
            calls.append(("sendall", data))

        def recv(self, size):  # pragma: no cover - 不该被调用
            calls.append(("recv", size))
            return b""

    def fake_connect(address, timeout=None):
        calls.append(("connect", address, timeout))
        return FakeSock()

    monkeypatch.setattr(rtsp.socket, "create_connection", fake_connect)
    assert port_listening("192.168.31.27") is True
    assert [item[0] for item in calls] == ["connect"], f"只该有 TCP 连接：{calls}"


def test_端口状态必须区分被拒与没有回应(monkeypatch):
    """**回归**（2026-09-30 真机）：X1C 的链路会丢包 —— 两次探测里有一次超时。

    若把"超时"当成"端口没在监听"，网络一抖画面就永远起不来
    （用户看到黑屏，界面却说他没开「局域网实时画面」）。所以只有**明确被拒**（RST）
    才算 ``closed``，超时算 ``unknown``，仍然照常去开流。
    """

    def refused(address, timeout=None):
        raise ConnectionRefusedError(10061, "connection refused")

    monkeypatch.setattr(rtsp.socket, "create_connection", refused)
    assert rtsp.port_state("192.168.31.27") == "closed"
    assert port_listening("192.168.31.27") is False

    def timeout(address, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(rtsp.socket, "create_connection", timeout)
    assert rtsp.port_state("192.168.31.27") == "unknown", "超时只说明问不出来，不等于端口关闭"
    assert port_listening("192.168.31.27") is False, "旧 API 只认 open"


def test_退避是有耐心的():
    """契约：连续失败时退避要长得起来（实测：越急越拿不到画面）。"""
    assert RTSPS_BACKOFF_FACTOR > 1.0
    assert RTSPS_BACKOFF_MAX >= 10.0, "3 秒一次地捅一台打印机只会把画面通道搅乱"
    assert RTSPS_BACKOFF_MAX > RTSP_RETRY_PAUSE
    assert RTSPS_HEALTHY_SECONDS >= RTSP_RETRY_PAUSE


# --------------------------------------------------------------------------- 交付策略
def test_第一帧一定编码():
    """契约：界面/网页都在等首帧，不能因为"还没人取过帧"就一直不编码。"""
    stream = _stream()
    assert stream._encode_due(time.time()) is True


def test_没人看的时候不再编码(monkeypatch):
    """契约：整面墙最小化时不该继续烧 CPU 编码（720p 解码+编码约 0.35 个核）。"""
    stream = _stream()
    stream._last_encode_ts = time.time() - 1.0
    stream._wanted_ts = time.time() - (RTSPS_DELIVERY_IDLE + 1.0)
    assert stream._encode_due(time.time()) is False

    # 有人来取过帧（界面在刷新）就继续编
    stream.latest_frame()
    assert stream._encode_due(time.time()) is True


def test_有人在看时按每路帧率交付():
    """契约：``max_fps`` 是**交付**帧率，不是"解码帧率"。"""
    stream = _stream()
    now = time.time()
    stream.latest_frame()  # 有人在看
    stream._max_fps = 4.0
    stream._last_encode_ts = now
    assert stream._encode_due(now + 0.05) is False, "4 fps = 每 250 ms 一张，不能每帧都编"
    assert stream._encode_due(now + 0.30) is True


def test_帧率设为零表示不限制交付():
    stream = _stream()
    stream.latest_frame()
    stream._max_fps = 0.0
    stream._last_encode_ts = time.time()
    assert stream._encode_due(time.time()) is True


def test_交付帧率与解码帧率分开统计():
    """契约：``fps``（界面看到的）与 ``source_fps``（从打印机收到的）是两个数。

    用户把每路帧率调低时，界面上显示的应该是"我能看到多少帧"，
    而"流还在不在动"要用解码计数（看门狗靠它判断）。
    """
    stream = _stream()
    stream._started_at = time.time() - 10.0
    stream._frame_count = 300  # 解码 300 帧
    stream._delivered = 40  # 只交付 40 帧
    assert stream.source_fps == pytest.approx(30.0, rel=0.2)
    assert stream.fps == pytest.approx(4.0, rel=0.2)
    assert stream.frame_count == 300, "看门狗看的是解码计数"


def test_取帧会登记有人在看():
    stream = _stream()
    assert stream._wanted_ts == 0.0
    stream.latest_frame()
    assert stream._wanted_ts > 0.0


# --------------------------------------------------------------------------- 不再自己探 RTSP
def test_不再有自己发DESCRIBE的预探():
    """**回归**：预探会建出一条 RTSP 会话，占住那唯一一条画面通道。

    实测：连发 18 次预探不会打死正在跑的流，但它是**多余的一条连接**；
    真正的问题是"会话翻搅 + 快速重连"会把画面通道搅乱（同一位客户端的
    帧率从 30 fps 掉到 0.2 fps），所以预探整条路径被删掉了。
    """
    assert not hasattr(RtspStream, "preflight"), "预探（自建 TLS + DESCRIBE）已删除"
    source = (rtsp.__file__ or "")
    text = open(source, encoding="utf-8").read()
    assert "RTSP/1.0\\r\\n" not in text, "这个模块不该再自己拼 RTSP 请求（会建出会话）"
    assert "RTSPS_PREFLIGHT" not in text
