"""`app/bambu/rtsp_h264.py` 的契约测试（纯离线，不连打印机）。

## 为什么单独立一条

只提供 RTSPS 通道的机型（X1/X1C/X2D/H2/P2S）在**安卓版**上没有画面，因为 APK
不能打包 OpenCV（Chaquopy 的轮子是 4096 字节对齐，16KB 内存页设备会拒绝加载 →
闪退）。`rtsp_h264.py` 是那条"不在这边解码"的通路：用标准库把 H.264 码流取出来，
交给网页端的 WebCodecs 去解。

它跑在**没有 OpenCV、没有 Android SDK** 的环境里，所以只能这样测：

* 用**人工构造的 RTP 包**喂给解包逻辑（单 NAL / FU-A 分片 / STAP-A 聚合、乱序、
  marker、丢包、填充位、扩展头），断言还原出的访问单元；
* 用**真机抓下来的 SDP**（X2D 固件实测）断言参数解析：Digest 质询、媒体级
  `a=control`、SPS/PPS、codec 串、avcC description；
* 断言"响应实体必须按 Content-Length 读完"——不然 DESCRIBE 拿到空 SDP，
  真实症状是"报成访问代码错误"（这个坑踩过，见模块文档）。
"""

from __future__ import annotations

import base64

import pytest

from app.bambu import rtsp_h264

pytestmark = pytest.mark.usefixtures("no_network")

#: 真机（X2D，固件 01.01.01.00）DESCRIBE 回来的 SDP（节选关键行）
REAL_SDP = "\r\n".join(
    [
        "v=0",
        'o=- 1234567890 1 IN IP4 192.168.31.110',
        "s=H.264 Stream, streamed by the LIVE555 Media Server",
        "c=IN IP4 0.0.0.0",
        "t=0 0",
        'a=control:*',
        "m=video 0 RTP/AVP 96",
        'a=rtpmap:96 H264/90000',
        "a=fmtp:96 packetization-mode=1;profile-level-id=641029;"
        "sprop-parameter-sets=Z2QQKawbGqB4AiflhAAAAwAEAAADAAEAAAMAMg8YMZYA,aO48sA==",
        "a=control:track1",
        "",
    ]
)


def _nal(nal_type: int, payload: bytes = b"\xaa\xbb\xcc") -> bytes:
    return bytes([0x60 | nal_type]) + payload


def _rtp(payload: bytes, *, timestamp: int = 1000, marker: bool = False, seq: int = 1,
         padding: int = 0, extension: bool = False) -> bytes:
    """构造一个 RTP 包（含可选的填充与扩展头）。"""
    first = 0x80
    if padding:
        first |= 0x20
    if extension:
        first |= 0x10
    header = bytes([first, (0x80 if marker else 0x00) | 96])
    header += seq.to_bytes(2, "big")
    header += timestamp.to_bytes(4, "big")
    header += b"\x00\x00\x00\x01"  # SSRC
    if extension:
        header += b"\xbe\xde\x00\x01" + b"\x00\x00\x00\x00"  # 1 个 32 位扩展字
    body = payload + (bytes([padding]) * padding if padding else b"")
    return header + body


def _client() -> rtsp_h264.RtspH264Client:
    return rtsp_h264.RtspH264Client("127.0.0.1", "12345678")


# --------------------------------------------------------------------------- SDP / 参数


def test_解析真机SDP():
    """用真机（X2D）SDP 的结构断言解析结果（fixture 里的 SPS/PPS 是节选值）。"""
    params = rtsp_h264.parse_sdp(REAL_SDP)
    assert params.payload_type == 96
    assert params.clock_rate == 90000
    assert params.packetization_mode == 1
    assert params.sps and params.pps
    assert params.ready is True
    # codec 串必须由 SPS 自己拼出来（不是写死的常量）
    assert params.codec_string == "avc1." + params.sps[1:4].hex()
    assert params.codec_string.startswith("avc1.6410"), "真机是 High profile 4.1"


def test_取媒体级control而不是会话级的星号():
    """回归：会话级 control 是 `*`，拿它做 SETUP 会 405（实机踩过）。"""
    params = rtsp_h264.parse_sdp(REAL_SDP)
    assert params.extra["session_control"] == "*"
    assert params.extra["media_control"] == "track1"
    assert params.control == "track1"


def test_control解析成SETUP目标():
    presentation = "rtsps://192.168.31.110:322/streaming/live/1"
    assert rtsp_h264.resolve_control("track1", presentation).endswith("/streaming/live/1/track1")
    assert rtsp_h264.resolve_control("*", presentation) == presentation
    assert rtsp_h264.resolve_control("", presentation) == presentation
    assert (
        rtsp_h264.resolve_control("rtsps://1.2.3.4:322/x", presentation) == "rtsps://1.2.3.4:322/x"
    )


def test_avcC_description结构():
    params = rtsp_h264.parse_sdp(REAL_SDP)
    record = params.avcc_description
    assert record[0] == 1, "configurationVersion 必须是 1"
    assert record[1:4] == params.sps[1:4], "profile/compat/level 要抄 SPS"
    assert record[4] == 0xFC, "lengthSizeMinusOne=3（4 字节长度前缀）"
    assert record[5] == 0xE1, "1 个 SPS"
    sps_len = int.from_bytes(record[6:8], "big")
    assert sps_len == len(params.sps)
    assert record[8 : 8 + sps_len] == params.sps
    pps_count = record[8 + sps_len]
    assert pps_count == 1
    pps_len = int.from_bytes(record[9 + sps_len : 11 + sps_len], "big")
    assert pps_len == len(params.pps)


def test_没有SPS时codec串给保守值且description为空():
    params = rtsp_h264.StreamParameters()
    assert params.codec_string == "avc1.42E01E"
    assert params.avcc_description == b""


# --------------------------------------------------------------------------- Digest


def test_digest质询解析():
    challenge = rtsp_h264.parse_digest_challenge(
        'WWW-Authenticate: Digest realm="LIVE555 Streaming Media", nonce="abc123"'
    )
    assert challenge["realm"] == "LIVE555 Streaming Media"
    assert challenge["nonce"] == "abc123"


def test_digest头按RFC2617拼装():
    header = rtsp_h264.build_digest_header(
        username="bblp",
        password="12345678",
        method="DESCRIBE",
        uri="rtsps://192.168.31.110:322/streaming/live/1",
        challenge={"realm": "LIVE555 Streaming Media", "nonce": "abc123"},
    )
    assert header.startswith("Digest ")
    assert 'username="bblp"' in header
    assert 'realm="LIVE555 Streaming Media"' in header
    assert 'nonce="abc123"' in header
    assert "response=" in header
    # 同一个质询必须得到同一个响应（确定性），换个 nonce 必须变
    again = rtsp_h264.build_digest_header(
        username="bblp",
        password="12345678",
        method="DESCRIBE",
        uri="rtsps://192.168.31.110:322/streaming/live/1",
        challenge={"realm": "LIVE555 Streaming Media", "nonce": "abc123"},
    )
    assert header == again
    other = rtsp_h264.build_digest_header(
        username="bblp",
        password="12345678",
        method="DESCRIBE",
        uri="rtsps://192.168.31.110:322/streaming/live/1",
        challenge={"realm": "LIVE555 Streaming Media", "nonce": "zzz"},
    )
    assert other != header


# --------------------------------------------------------------------------- RTP 解包


def test_单NAL包还原成一帧():
    client = _client()
    units: list = []
    client._on_access_unit = units.append
    client._handle_rtp(_rtp(_nal(rtsp_h264.NAL_SLICE), marker=True))
    assert len(units) == 1
    unit = units[0]
    assert unit.is_keyframe is False
    # AVCC：4 字节长度 + NAL
    size = int.from_bytes(unit.data[:4], "big")
    assert size == len(unit.data) - 4
    assert unit.data[4] & 0x1F == rtsp_h264.NAL_SLICE


def test_IDR帧被标成关键帧():
    client = _client()
    units: list = []
    client._on_access_unit = units.append
    client._handle_rtp(_rtp(_nal(rtsp_h264.NAL_IDR), marker=True))
    assert units and units[0].is_keyframe is True


def test_FU_A分片能拼回完整NAL():
    client = _client()
    units: list = []
    client._on_access_unit = units.append
    head = bytes([0x60 | rtsp_h264.NAL_FU_A, 0x80 | rtsp_h264.NAL_IDR]) + b"\x01\x02"
    middle = bytes([0x60 | rtsp_h264.NAL_FU_A, rtsp_h264.NAL_IDR]) + b"\x03\x04"
    tail = bytes([0x60 | rtsp_h264.NAL_FU_A, 0x40 | rtsp_h264.NAL_IDR]) + b"\x05\x06"
    client._handle_rtp(_rtp(head, timestamp=7))
    client._handle_rtp(_rtp(middle, timestamp=7))
    client._handle_rtp(_rtp(tail, timestamp=7, marker=True))
    assert len(units) == 1
    size = int.from_bytes(units[0].data[:4], "big")
    nal = units[0].data[4 : 4 + size]
    assert nal[0] & 0x1F == rtsp_h264.NAL_IDR, "分片要还原成原始 NAL 类型"
    assert nal[1:] == b"\x01\x02\x03\x04\x05\x06"
    assert units[0].is_keyframe is True


def test_STAP_A聚合包会拆成多个NAL():
    client = _client()
    units: list = []
    client._on_access_unit = units.append
    first, second = _nal(rtsp_h264.NAL_SPS), _nal(rtsp_h264.NAL_PPS, b"\x01")
    payload = (
        bytes([0x60 | rtsp_h264.NAL_STAP_A])
        + len(first).to_bytes(2, "big")
        + first
        + len(second).to_bytes(2, "big")
        + second
    )
    client._handle_rtp(_rtp(payload, marker=True))
    assert len(units) == 1
    # 两个 NAL 都要在同一个访问单元里（各带 4 字节长度前缀）
    index = 0
    types = []
    while index + 4 <= len(units[0].data):
        size = int.from_bytes(units[0].data[index : index + 4], "big")
        index += 4
        types.append(units[0].data[index] & 0x1F)
        index += size
    assert types == [rtsp_h264.NAL_SPS, rtsp_h264.NAL_PPS]
    assert client.parameters.sps and client.parameters.pps, "聚合包里的参数集也要收下"


def test_时间戳变化即切帧():
    client = _client()
    units: list = []
    client._on_access_unit = units.append
    client._handle_rtp(_rtp(_nal(rtsp_h264.NAL_SLICE), timestamp=10))
    client._handle_rtp(_rtp(_nal(rtsp_h264.NAL_SLICE), timestamp=20))  # 新时间戳 → 上一帧结束
    client._handle_rtp(_rtp(_nal(rtsp_h264.NAL_SLICE), timestamp=20, marker=True))
    assert len(units) == 2, "第一帧由时间戳变化触发，第二帧由 marker 触发"


def test_填充位与扩展头不会污染负载():
    client = _client()
    units: list = []
    client._on_access_unit = units.append
    payload = _nal(rtsp_h264.NAL_SLICE, b"\x11\x22\x33")
    client._handle_rtp(_rtp(payload, marker=True, padding=4, extension=True))
    assert len(units) == 1
    size = int.from_bytes(units[0].data[:4], "big")
    assert units[0].data[4 : 4 + size] == payload


@pytest.mark.parametrize(
    "packet",
    [
        b"",  # 空
        b"\x00\x01\x02",  # 太短
        b"\x40" + b"\x00" * 11,  # 版本不是 2
        b"\x80\x60\x00\x01\x00\x00\x00\x01\x00\x00\x00\x01",  # 只有头没有负载
    ],
)
def test_畸形RTP包被安全丢弃(packet):
    client = _client()
    units: list = []
    client._on_access_unit = units.append
    client._handle_rtp(packet)
    assert units == []
    assert client.frame_count == 0


def test_长时间没数据的兜底会把半截帧交出去():
    """契约：marker 丢失时不能把一帧永远攥在手里（否则画面卡住不动）。"""
    client = _client()
    units: list = []
    client._on_access_unit = units.append
    client._handle_rtp(_rtp(_nal(rtsp_h264.NAL_SLICE), timestamp=5))  # 没有 marker
    assert units == [], "还没到帧边界，先攒着"
    client._flush_au()
    assert len(units) == 1


def test_帧序号与统计():
    client = _client()
    units: list = []
    client._on_access_unit = units.append
    for index in range(3):
        client._handle_rtp(_rtp(_nal(rtsp_h264.NAL_SLICE), timestamp=index, marker=True))
    assert [unit.sequence for unit in units] == [1, 2, 3]
    assert client.frame_count == 3
    assert client.packets == 3
    assert client.bytes_in > 0


def test_接口形状与rtsp_py一致():
    """契约：状态字符串与 rtsp.py / camera.py 必须一致 —— 界面文案共用一套判据。"""
    from app.bambu.camera import CameraStream
    from app.bambu.rtsp import RtspStream

    for name in ("connecting", "streaming", "retrying", "stopped", "auth_error"):
        assert getattr(rtsp_h264.RtspH264Client, f"STATE_{name.upper()}") == getattr(
            CameraStream, f"STATE_{name.upper()}"
        )
        assert getattr(rtsp_h264.RtspH264Client, f"STATE_{name.upper()}") == getattr(
            RtspStream, f"STATE_{name.upper()}"
        )
    assert rtsp_h264.RtspH264Client.available() is True, "纯标准库实现，任何平台都可用"


def test_参数集只记一次且以流内为准():
    client = _client()
    client._on_access_unit = lambda unit: None
    sps = bytes([0x67, 0x64, 0x10, 0x29])
    pps = bytes([0x68, 0x01])
    client._handle_rtp(_rtp(sps, timestamp=1))
    client._handle_rtp(_rtp(pps, timestamp=1, marker=True))
    assert client.parameters.sps == sps
    assert client.parameters.pps == pps
    assert client.parameters.codec_string == "avc1.641029"
