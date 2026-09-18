"""H.264 码流交付通路的接口层测试（``app/web/server.py`` 的 `KIND_H264`）。

背景：安卓版刻意不内置 OpenCV（Chaquopy 的 opencv/numpy 轮子是 4096 字节对齐，
16KB 内存页的设备拒绝加载 → 直接闪退），而 X1/X2D/H2/P2S 这些机型**只有 RTSPS(322)**
画面通道。于是服务端不解码，只把码流（AVCC 格式的访问单元）推给网页端，
由 WebView 自带的 WebCodecs（`VideoDecoder`）解出来画 canvas。

这里锁定的契约：

* ``/api/printers`` 每台设备带 ``video_mode``（``jpeg`` / ``h264``）；
* `KIND_H264` 记录负载：首字节 ``0x01`` = 初始化参数（JSON，description 是 base64）、
  ``0x02`` = 增量帧、``0x03`` = 关键帧；
* 初始化参数一路只推一次，且必须排在任何访问单元**之前**（WebCodecs 要求先 configure）；
* `/api/live` 里走 H.264 的那一路**不再去取 JPEG 帧**（没有 OpenCV 时本来也取不到）。

所有请求只走 127.0.0.1 与临时端口，不依赖公网与真实打印机。
"""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from app.bambu.models import PrinterInfo, PrinterModel, PrinterStatus  # noqa: E402
from app.web import server as web_server  # noqa: E402
from app.web.server import WebServer  # noqa: E402

#: 导入期先抓住「真」socket 方法（conftest 的 no_network 夹具会在测试期把它们换掉）
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_BIND = socket.socket.bind
_REAL_LISTEN = socket.socket.listen
_REAL_CREATE_CONNECTION = socket.create_connection

#: 一份真实的 avcC（X2D 实测：SPS 27 字节 + PPS 4 字节）——只要内容可往返即可
DESCRIPTION = bytes.fromhex("01641029ffe1000967641029acd94078022701000468ee3c80")
CODEC = "avc1.641029"


def _is_loopback(address) -> bool:
    host = str(address[0] if isinstance(address, tuple) and address else "")
    return host in ("", "localhost", "::1") or host.startswith("127.")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """与 ``test_web_server.py`` 同样的窄守卫：只放行回环地址。"""

    def tcp_guard(original, name):
        def wrapper(self, address, *args, **kwargs):
            is_tcp = getattr(self, "type", None) == socket.SOCK_STREAM
            if is_tcp and not _is_loopback(address):
                raise RuntimeError(f"测试禁止连接非回环 TCP 地址（{name} {address!r}）")
            return original(self, address, *args, **kwargs)

        return wrapper

    def create_connection_guard(address, *args, **kwargs):
        if not _is_loopback(address):
            raise RuntimeError(f"测试禁止连接非回环地址（create_connection {address!r}）")
        return _REAL_CREATE_CONNECTION(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", tcp_guard(_REAL_CONNECT, "connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", tcp_guard(_REAL_CONNECT_EX, "connect_ex"))
    monkeypatch.setattr(socket, "create_connection", create_connection_guard)
    monkeypatch.setattr(socket.socket, "bind", _REAL_BIND)
    monkeypatch.setattr(socket.socket, "listen", _REAL_LISTEN)


class Unit:
    """``app.bambu.rtsp_h264.AccessUnit`` 的替身（只用到这两个字段）。"""

    def __init__(self, data: bytes, is_keyframe: bool) -> None:
        self.data = data
        self.is_keyframe = is_keyframe
        self.timestamp_us = 0
        self.sequence = 0


class FakeH264Session:
    """走纯 Python H.264 通路的会话替身。"""

    def __init__(self, units: list[Unit] | None = None, params: dict | None = None) -> None:
        self.info = PrinterInfo(
            ip="127.0.0.1",
            serial="20P00A1234567",
            name="车间 X2D",
            model=PrinterModel.X2D,
            access_code="12345678",
        )
        self.capabilities = self.info.model.capabilities
        self.can_control = True
        self.controls_blocked_reason = ""
        self.controls_blocked_short = ""
        self.video_unavailable_reason = ""
        self.video_backend = "RTSP"
        self.camera_fps = 0.0
        self.mqtt_auth_error = False
        self.last_camera_state = "streaming"
        self.last_camera_detail = "分片 H.264 已连接"
        self.video_mode = "h264"
        self._params = (
            {"codec": CODEC, "description": DESCRIPTION, "width": 0, "height": 0}
            if params is None
            else params
        )
        self._units = list(units or [])
        self.latest_calls = 0
        self.frames_requested = 0

    def snapshot(self) -> PrinterStatus:
        return PrinterStatus(mqtt_online=True, camera_online=True)

    def latest_frame(self) -> tuple[int, bytes | None]:
        self.frames_requested += 1
        return 0, None

    def latest_h264(self) -> tuple[dict, list[Unit]]:
        self.latest_calls += 1
        params, self._params = self._params, {}
        units, self._units = self._units, []
        return params, units


class JpegSession(FakeH264Session):
    """普通会话：走 MJPEG 那一路，`video_mode` 必须是 jpeg。"""

    def __init__(self) -> None:
        super().__init__()
        self.video_mode = "jpeg"


class OldSession:
    """老会话替身：连 `latest_h264` 都没有（`_push_h264` 要能容忍）。"""

    def __init__(self) -> None:
        self.info = PrinterInfo(
            ip="127.0.0.1",
            serial="01P00A1234567",
            name="老机型",
            model=PrinterModel.P1S,
            access_code="12345678",
        )
        self.capabilities = self.info.model.capabilities
        self.can_control = True
        self.video_backend = "TCP6000"
        self.camera_fps = 12.0
        self.mqtt_auth_error = False
        self.last_camera_state = "streaming"
        self.last_camera_detail = ""

    def snapshot(self) -> PrinterStatus:
        return PrinterStatus(mqtt_online=True, camera_online=True)

    def latest_frame(self) -> tuple[int, bytes | None]:
        return 0, None


# ---------------------------------------------------------------------- 工具
def parse_records(data: bytes) -> list[tuple[int, int, bytes]]:
    """按**网页端 `processBuffer` 的规则**解析记录（独立实现，不复用服务端代码）。"""
    records: list[tuple[int, int, bytes]] = []
    offset = 0
    while True:
        while offset + 1 < len(data) and data[offset : offset + 2] != web_server.RECORD_MAGIC:
            offset += 1
        if offset + web_server.RECORD_HEADER > len(data):
            break
        kind = data[offset + 2]
        index = int.from_bytes(data[offset + 3 : offset + 5], "little")
        length = int.from_bytes(data[offset + 5 : offset + 9], "little")
        if offset + web_server.RECORD_HEADER + length > len(data):
            break
        start = offset + web_server.RECORD_HEADER
        records.append((kind, index, data[start : start + length]))
        offset = start + length
    return records


def handler_for(session, wfile: io.BytesIO | None = None):
    """构造一个只够调 `_push_h264` 的处理器（不碰 socket）。"""
    server = WebServer(get_sessions=lambda: [session], port=0, host="127.0.0.1")
    server.app = server  # type: ignore[attr-defined]
    handler = object.__new__(web_server._Handler)
    handler.server = server
    handler.wfile = wfile if wfile is not None else io.BytesIO()
    return handler


def push(session, sent: set[int] | None = None):
    """调一次 `_push_h264`，返回 (是否继续, 解析出的记录)。"""
    handler = handler_for(session)
    ok = web_server._Handler._push_h264(handler, 0, session, sent if sent is not None else set())
    return ok, parse_records(handler.wfile.getvalue())


def serve(sessions):
    holder = list(sessions)
    server = WebServer(
        get_sessions=lambda: list(holder),
        port=0,
        token="",
        fps=4.0,
        max_width=720,
        host="127.0.0.1",
    )
    assert server.start() is True
    server.port = server._httpd.server_address[1]
    return server, server.port


# ------------------------------------------------------------------ video_mode
def test_每台设备都带video_mode():
    """契约：`/api/printers` 必须有 `video_mode`，前端据此选 `<img>` 还是 WebCodecs。"""
    import http.client

    server, port = serve([FakeH264Session(), JpegSession(), OldSession()])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
        try:
            conn.request("GET", "/api/printers")
            response = conn.getresponse()
            assert response.status == 200
            items = json.loads(response.read().decode("utf-8"))["printers"]
        finally:
            conn.close()
    finally:
        server.stop()

    assert [item["video_mode"] for item in items] == ["h264", "jpeg", "jpeg"], (
        "只有真的走纯 Python H.264 通路时才是 h264；没有该属性（老会话）要按 jpeg 处理"
    )


def test_video_mode取不到时按jpeg处理():
    """契约：`video_mode` 缺失/异常一律当 jpeg，绝不能让画面区域整个空掉。"""
    assert web_server._video_mode_of(OldSession()) == "jpeg"
    assert web_server._video_mode_of(FakeH264Session()) == "h264"

    class Weird:
        video_mode = None

    assert web_server._video_mode_of(Weird()) == "jpeg"


# --------------------------------------------------------------- 记录格式
def test_初始化参数先于访问单元且只推一次():
    """契约：``0x01`` 参数必须在任何访问单元之前，并且一路只推一次。

    WebCodecs 要求先 `configure()` 才收 chunk；参数若排在帧后面，
    网页端只能先把帧攒着（我们做了缓冲，但顺序仍必须是参数在前）。
    """
    units = [Unit(b"\x00\x00\x00\x04\x65\x88\x84\x00", True), Unit(b"\x00\x00\x00\x02\x41\x9a", False)]
    session = FakeH264Session(units)
    sent: set[int] = set()

    ok, records = push(session, sent)
    assert ok is True
    kinds = [record[0] for record in records]
    assert kinds[0] == web_server.KIND_H264, "H.264 必须走 KIND_H264 记录"
    assert web_server.KIND_H264 == 3, "记录类型号是前后端契约"
    payloads = [record[2] for record in records]
    assert payloads[0][0] == 0x01, "第一条必须是初始化参数"
    assert [payload[0] for payload in payloads[1:]] == [0x03, 0x02], (
        "关键帧要标 0x03、增量帧标 0x02（网页端据此设置 EncodedVideoChunk.type，"
        "标错会让解码器在关键帧之前收到 delta 而整段报错）"
    )
    assert all(record[1] == 0 for record in records), "画面序号要回填给前端"

    # 参数只在开头推一次
    ok, again = push(session, sent)
    assert ok is True
    assert [record[2][0] for record in again] == [], "参数重复推会让网页端反复重建解码器"


def test_初始化参数是网页端能直接用的JSON():
    """契约：``0x01`` 的负载是 JSON，`description` 是 base64 的 avcC，可原样还原。"""
    session = FakeH264Session([Unit(b"\x00\x00\x00\x04\x65\x88\x84\x00", True)])
    _, records = push(session)
    payload = records[0][2]
    assert payload[0:1] == b"\x01"
    params = json.loads(payload[1:].decode("utf-8"))
    assert params["codec"] == CODEC, "codec 串要能直接喂给 VideoDecoder.configure"
    assert base64.b64decode(params["description"]) == DESCRIPTION, (
        "description 必须能还原成 avcC：WebCodecs 靠它拿 SPS/PPS，丢了就解不出画面"
    )
    assert set(params) == {"codec", "description", "width", "height"}


def test_没有H264能力的老会话不受影响():
    """契约：老会话（连 `latest_h264` 都没有）不产生任何记录，也不能报错。"""
    ok, records = push(OldSession())
    assert ok is True and records == []


def test_对端断开时返回False让实时循环收尾():
    """契约：写记录失败（对端关了页面）要返回 False，让 `_live` 直接退出。

    否则这条连接会一直空转，`WebFrameCache` 的在线客户端计数也永远不归零。
    """

    class Broken(io.BytesIO):
        def write(self, _data):  # type: ignore[override]
            raise BrokenPipeError(10053, "对端已关闭")

    session = FakeH264Session([Unit(b"\x00\x00\x00\x04\x65\x88\x84\x00", True)])
    handler = handler_for(session, Broken())
    assert web_server._Handler._push_h264(handler, 0, session, set()) is False


# ------------------------------------------------------------------ 端到端
def test_实时通道真的把H264推出来且不再取JPEG帧():
    """端到端：`/api/live` 上能收到「参数 + 关键帧」，且该路不再去要 JPEG 帧。"""
    units = [
        Unit(b"\x00\x00\x00\x04\x65\x88\x84\x00", True),
        Unit(b"\x00\x00\x00\x02\x41\x9a", False),
    ]
    session = FakeH264Session(units)
    server, port = serve([session])
    try:
        conn = socket.create_connection(("127.0.0.1", port), timeout=8)
        try:
            conn.sendall(b"GET /api/live HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            data = b""
            deadline = time.time() + 8
            while time.time() < deadline:
                conn.settimeout(1.0)
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                data += chunk
                if len(parse_records(data.split(b"\r\n\r\n", 1)[-1])) >= 3:
                    break
        finally:
            conn.close()
    finally:
        server.stop()

    body = data.split(b"\r\n\r\n", 1)[-1]
    assert b"200 OK" in data, data[:200]
    records = parse_records(body)
    h264 = [record[2] for record in records if record[0] == web_server.KIND_H264]
    assert len(h264) >= 3, f"实时通道没把 H.264 推出来：{records!r}"
    assert h264[0][0] == 0x01 and [payload[0] for payload in h264[1:3]] == [0x03, 0x02]
    assert session.frames_requested == 0, (
        "走 H.264 的那一路不该再去要 JPEG 帧：没有 OpenCV 时那是白等（还会拖慢其他路）"
    )
