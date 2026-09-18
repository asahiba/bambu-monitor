"""内置网页服务的接口层测试（``app/web/server.py``）。

覆盖的契约：

* 令牌鉴权：``?token=`` / ``Cookie: bm_token=`` / ``/health`` 免鉴权 / 错误令牌 401
* 路由与静态资源：``/``、``/index.html``、``/manifest.webmanifest``、``/sw.js``、
  ``/icon-192.png``、未知路径 404
* ``/api/printers`` 的字段与取值钳制（progress 0..100、span 1..3、ams/hms 为列表）
* ``POST /api/command`` 的控制指令、``can_control`` 门禁、非法 JSON 与未知指令
* ``/api/frame/<i>`` 的单帧快照（无帧 503 / 有帧 image/jpeg）
* ``/stream/<i>`` 的 multipart 响应头
* ``WebServer.stop()`` 之后可以再次 ``start()``，且重启保留 ``max_width``
* ``WebServer.urls()`` 的回环地址与令牌拼接

全部请求只走 127.0.0.1 与系统分配的临时端口（``port=0``），不依赖公网与真实打印机。
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys

# 必须在导入 PySide6 / 应用模块之前设置，避免无显示环境下 Qt 初始化失败
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest

from app.bambu.models import AmsTray, HmsItem, PrinterInfo, PrinterModel, PrinterStatus
from app.web.page import INDEX_HTML
from app.web.server import WebServer

TOKEN = "secret"

#: 导入期先抓住「真」socket 方法：conftest.py 的 no_network 夹具会在测试期把它们换掉
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_BIND = socket.socket.bind
_REAL_LISTEN = socket.socket.listen
_REAL_CREATE_CONNECTION = socket.create_connection


def _is_loopback(address) -> bool:
    host = address[0] if isinstance(address, tuple) and address else ""
    host = str(host)
    return host in ("", "localhost", "::1") or host.startswith("127.")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """覆盖 tests/conftest.py 里的同名 autouse 夹具。

    本文件属于「I/O 与接口层」：按任务约定必须用 127.0.0.1 回环地址 + 系统分配的
    临时端口（``port=0``）来验证 HTTP 契约，而 conftest 的全局 no_network 会拦掉
    一切 bind/connect。这里换成一份更窄的守卫，**依然禁止任何非回环连接**
    （真实打印机、公网都不可能被碰到）：

    * TCP ``connect``/``connect_ex``/``create_connection``：只放行回环地址；
    * UDP ``connect``：放行（UDP connect 不发包，``local_interfaces()`` 用它探测默认路由）；
    * ``bind``/``listen``：放行（回环服务必须绑定；绑定了也不会主动连任何设备）。
    """

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
    # 无论 conftest 里的联网守卫叫什么名字，本文件都需要真实的本地监听能力
    monkeypatch.setattr(socket.socket, "bind", _REAL_BIND)
    monkeypatch.setattr(socket.socket, "listen", _REAL_LISTEN)


#: 一个极小的假 JPEG：只用于验证「原样透传」，不会被解码
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 2048 + b"\xff\xd9"
#: 超过 PASSTHROUGH_BYTES(90000) 才需要转码，这里保持小体积走透传分支
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class FakeSession:
    """``PrinterSession`` 的最小替身：只实现网页服务真正会调用的成员。"""

    def __init__(
        self,
        status: PrinterStatus | None = None,
        info: PrinterInfo | None = None,
        can_control: bool = True,
        frame: bytes | None = None,
        control_ok: bool = True,
    ) -> None:
        self._status = status if status is not None else PrinterStatus()
        self.info = info or PrinterInfo(
            ip="127.0.0.1",
            serial="01P00A1234567",
            name="测试机",
            model=PrinterModel.P1S,
            access_code="12345678",
        )
        self.can_control = can_control
        # 能力声明：真实会话由「机型固有能力 + 运行时观测」得出，这里取该机型的固有能力
        self.capabilities = self.info.model.capabilities
        self.controls_blocked_reason = ""
        self.video_backend = "TCP6000"
        self.camera_fps = 12.5
        self.mqtt_auth_error = False
        self.last_camera_state = "streaming"
        self.last_camera_detail = "视频已连接"
        self._frame = frame
        self._control_ok = control_ok
        #: 记录被调用过的控制方法，供断言
        self.calls: list[tuple] = []

    # ------------------------------------------------------------ 只读接口
    def snapshot(self) -> PrinterStatus:
        return self._status

    def latest_frame(self) -> tuple[int, bytes | None]:
        if self._frame is None:
            return 0, None
        return 1, self._frame

    # ------------------------------------------------------------ 控制接口
    def pause_print(self) -> bool:
        self.calls.append(("pause",))
        return self._control_ok

    def resume_print(self) -> bool:
        self.calls.append(("resume",))
        return self._control_ok

    def stop_print(self) -> bool:
        self.calls.append(("stop",))
        return self._control_ok

    def set_light(self, on: bool) -> bool:
        self.calls.append(("light", bool(on)))
        return self._control_ok

    def set_speed(self, level: int) -> bool:
        self.calls.append(("speed", int(level)))
        return self._control_ok


def online_status(progress: int = 42, span: int = 1) -> PrinterStatus:
    """构造一个「在线打印中」的状态快照。"""
    return PrinterStatus(
        mqtt_online=True,
        camera_online=True,
        gcode_state="RUNNING",
        progress=progress,
        remaining_minutes=63,
        nozzle_temper=25.6,
        nozzle_target_temper=220.0,
        bed_temper=59.4,
        bed_target_temper=60.0,
        wifi_signal="-52dBm",
        lights={"chamber_light": "on"},
        subtask_name="测试件.gcode.3mf",
        layer_num=12,
        total_layer_num=240,
        ams_trays=[
            AmsTray(
                ams_id=0,
                tray_id=0,
                tray_type="PLA",
                color="FF3B30FF",
                remain=50,
                has_rfid=True,
            )
        ],
        hms_items=[
            HmsItem(
                attr=0x03000100,
                code=0x00010001,
                text="",
                wiki="https://wiki.bambulab.com/x",
                short_code="HMS_0300_0100_0001_0001",
            )
        ],
    )


def online_info(span: int = 1) -> PrinterInfo:
    return PrinterInfo(
        ip="127.0.0.1",
        serial="01P00A1234567",
        name="测试机",
        model=PrinterModel.P1S,
        access_code="12345678",
        tile_span=span,
    )


# ---------------------------------------------------------------------- 工具
def _serve(sessions=None, token: str = "", max_width: int = 720, fps: float = 4.0):
    """起一个网页服务的上下文管理器：端口 0 由系统分配，退出时必定关闭。"""
    holder = list(sessions or [])
    server = WebServer(
        get_sessions=lambda: list(holder),
        port=0,
        token=token,
        fps=fps,
        max_width=max_width,
        host="127.0.0.1",
    )
    assert server.start() is True, "服务应当能在回环地址的临时端口上启动"
    # port=0 时真实端口由系统分配，回填后再测 urls()
    server.port = server._httpd.server_address[1]
    try:
        yield server, server.port
    finally:
        server.stop()


@pytest.fixture(scope="module")
def plain_server():
    """模块级共享服务（无令牌、无设备）：静态资源/路由这类只读用例共用它，
    避免每个用例都付一次 serve_forever 的停机轮询开销。"""
    yield from _serve([])


@pytest.fixture(scope="module")
def token_server():
    """模块级共享服务（``token="secret"``、无设备）：鉴权类只读用例共用。"""
    yield from _serve([], token=TOKEN)


@pytest.fixture()
def make_server():
    """按用例创建网页服务（需要自定义 session / 令牌 / 宽度时使用），测试结束统一关闭。"""
    created: list[WebServer] = []

    def _make(
        sessions: list[FakeSession] | None = None,
        token: str = "",
        max_width: int = 720,
        fps: float = 4.0,
    ) -> tuple[WebServer, int]:
        holder = list(sessions or [])
        server = WebServer(
            get_sessions=lambda: list(holder),
            port=0,
            token=token,
            fps=fps,
            max_width=max_width,
            host="127.0.0.1",
        )
        assert server.start() is True, "服务应当能在回环地址的临时端口上启动"
        created.append(server)
        server.port = server._httpd.server_address[1]
        return server, server.port

    yield _make

    for server in created:
        server.stop()


def request(
    port: int,
    path: str,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """发一次 HTTP 请求，返回 (状态码, 响应头, 响应体)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        payload = response.read()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, payload
    finally:
        conn.close()


def post_command(port: int, payload, token: str = "", raw: bytes | None = None):
    """POST /api/command 的便捷封装。"""
    query = f"?token={token}" if token else ""
    body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    return request(
        port,
        f"/api/command{query}",
        method="POST",
        body=body,
        headers={"Content-Type": "application/json"},
    )


# ---------------------------------------------------------------------- 鉴权
def test_health_不需要令牌(token_server):
    """契约：/health 是存活检查接口，带令牌配置时也不需要 token，返回 200 + "ok"。"""
    _, port = token_server
    status, headers, body = request(port, "/health")
    assert status == 200
    assert body == b"ok"
    assert headers["content-type"].startswith("text/plain")


def test_无令牌访问首页返回401(token_server):
    """契约：配置了 token 时，不带令牌访问首页返回 401 且响应体是 JSON 错误。"""
    _, port = token_server
    status, headers, body = request(port, "/")
    assert status == 401
    assert headers["content-type"].startswith("application/json")
    assert json.loads(body.decode("utf-8"))["error"] == "unauthorized"


def test_查询参数令牌通过(token_server):
    """契约：?token=<正确令牌> 可以访问受保护页面。"""
    _, port = token_server
    status, _, body = request(port, f"/?token={TOKEN}")
    assert status == 200
    assert body.decode("utf-8") == INDEX_HTML


def test_cookie令牌通过(token_server):
    """契约：Cookie: bm_token=<正确令牌> 等效于查询参数令牌。"""
    _, port = token_server
    status, _, body = request(port, "/", headers={"Cookie": f"other=1; bm_token={TOKEN}; x=2"})
    assert status == 200
    assert body.decode("utf-8") == INDEX_HTML


def test_错误令牌返回401(token_server):
    """契约：令牌不匹配一律 401（查询参数与 Cookie 都是）。"""
    _, port = token_server
    status, _, _ = request(port, "/?token=wrong")
    assert status == 401
    status, _, _ = request(port, "/", headers={"Cookie": "bm_token=wrong"})
    assert status == 401


def test_未配置令牌时直接放行(plain_server):
    """契约：token 为空表示不启用鉴权，任何请求都能通过。"""
    _, port = plain_server
    status, _, _ = request(port, "/")
    assert status == 200


# ---------------------------------------------------------------------- 路由
def test_根路径与index_html返回页面(plain_server):
    """契约：/ 与 /index.html 都返回同一份 HTML 页面。"""
    _, port = plain_server
    for path in ("/", "/index.html"):
        status, headers, body = request(port, path)
        assert status == 200, path
        assert headers["content-type"].startswith("text/html")
        text = body.decode("utf-8")
        assert text == INDEX_HTML
        assert text.lstrip().lower().startswith("<!doctype html>")


def test_manifest返回JSON且含icons(token_server):
    """契约：/manifest.webmanifest 返回 PWA 清单，含 icons 数组与带令牌的 start_url。"""
    _, port = token_server
    status, headers, body = request(port, f"/manifest.webmanifest?token={TOKEN}")
    assert status == 200
    assert "manifest+json" in headers["content-type"]
    manifest = json.loads(body.decode("utf-8"))
    assert isinstance(manifest["icons"], list) and manifest["icons"]
    assert all("sizes" in item and "src" in item for item in manifest["icons"])
    assert manifest["start_url"] == f"/?token={TOKEN}"


def test_sw_js返回JavaScript(plain_server):
    """契约：/sw.js 返回 Service Worker 脚本（javascript MIME，非空）。"""
    _, port = plain_server
    status, headers, body = request(port, "/sw.js")
    assert status == 200
    assert "javascript" in headers["content-type"]
    assert b"addEventListener" in body


def test_icon_192返回PNG(plain_server):
    """契约：/icon-192.png 返回 image/png 且内容是真 PNG。"""
    _, port = plain_server
    status, headers, body = request(port, "/icon-192.png")
    assert status == 200
    assert headers["content-type"] == "image/png"
    assert body.startswith(PNG_MAGIC)
    assert len(body) > 0


def test_未知路径返回404(plain_server):
    """契约：未注册的路径返回 404，而不是 500 或静默 200。"""
    _, port = plain_server
    status, _, body = request(port, "/no-such-page")
    assert status == 404
    assert body == b"not found"


# ------------------------------------------------------------------ 状态接口
def test_api_printers_字段与钳制(make_server):
    """契约：/api/printers 顶层统计齐全；progress 夹在 0..100、span 夹在 1..3；
    ams/hms 始终是列表。"""
    hot = FakeSession(status=online_status(progress=250), info=online_info(span=9))
    cold_status = PrinterStatus(mqtt_online=False, camera_online=False, progress=-5)
    cold = FakeSession(
        status=cold_status,
        info=PrinterInfo(
            ip="127.0.0.1",
            serial="03900A2345678",
            name="离线机",
            model=PrinterModel.A1,
            access_code="12345678",
            tile_span=0,
        ),
        can_control=False,
    )
    cold.last_camera_state = "retrying"
    cold.last_camera_detail = "连接超时，请检查局域网模式"

    _, port = make_server(sessions=[hot, cold], fps=6.0)
    status, headers, body = request(port, "/api/printers")
    assert status == 200
    assert headers["content-type"].startswith("application/json")
    data = json.loads(body.decode("utf-8"))

    for key in ("total", "camera_online", "mqtt_online", "printing", "web_fps", "printers"):
        assert key in data, key
    assert data["total"] == 2
    assert data["web_fps"] == 6.0
    assert data["camera_online"] == 1
    assert data["mqtt_online"] == 1
    assert data["printing"] == 1
    assert len(data["printers"]) == 2

    first = data["printers"][0]
    assert first["index"] == 0
    assert first["progress"] == 100, "progress=250 必须被夹到 100"
    assert first["span"] == 3, "tile_span=9 必须被夹到 3"
    assert isinstance(first["ams"], list) and len(first["ams"]) == 1
    assert isinstance(first["hms"], list) and len(first["hms"]) == 1
    assert first["ams"][0]["label"] == "AMS1-1"
    assert first["ams"][0]["color"] == "#FF3B30"
    assert first["ams"][0]["remain_known"] is True
    assert first["ams"][0]["remain_text"] == "50%"
    # 文案表里查不到时给出兜底提示，而不是空字符串
    assert first["hms"][0]["code"] == "HMS_0300_0100_0001_0001"
    assert first["hms"][0]["text"] == "（本地文案表里没有这条错误码）"
    assert first["can_control"] is True
    assert first["status_text"] == "在线"
    assert first["state_text"] == "打印中"
    assert first["nozzle"] == "26" and first["bed"] == "59"
    assert first["light"] == "on"
    assert first["wifi_level"] == 4
    assert first["layer_text"] == "层 12/240"

    second = data["printers"][1]
    assert second["index"] == 1
    assert second["progress"] == 0, "progress=-5 必须被夹到 0"
    assert second["span"] == 1, "tile_span=0 必须被夹到 1"
    assert second["ams"] == [] and second["hms"] == []
    assert second["can_control"] is False
    # 状态行只放稳定的短标签（宽度可控），完整原因走 status_detail 给前端做悬浮提示。
    # 以前是 `detail[:14]`，中文提示会被截成「连接超时，请检查局」这种半句话。
    assert second["status_text"] == "画面重连中"
    assert second["status_detail"] == "连接超时，请检查局域网模式"
    assert second["state_text"] == "离线"


def test_api_printers_无设备时统计为零(plain_server):
    """契约：没有打印机时返回空列表且统计全为 0。"""
    _, port = plain_server
    status, _, body = request(port, "/api/printers")
    assert status == 200
    data = json.loads(body.decode("utf-8"))
    assert data["total"] == 0
    assert data["printers"] == []
    assert (data["camera_online"], data["mqtt_online"], data["printing"]) == (0, 0, 0)


# ------------------------------------------------------------------ 控制接口
def test_command_pause成功(make_server):
    """契约：POST /api/command {"index":0,"action":"pause"} 调到 session.pause_print()
    并返回 200 + {"ok":true}。"""
    session = FakeSession(status=online_status())
    _, port = make_server(sessions=[session])
    status, headers, body = post_command(port, {"index": 0, "action": "pause"})
    assert status == 200
    assert headers["content-type"].startswith("application/json")
    assert json.loads(body.decode("utf-8"))["ok"] is True
    assert ("pause",) in session.calls


def test_command_各动作分发正确(make_server):
    """契约：resume/stop/light_*/speed 动作分发给对应方法，速度档位夹在 1..4。"""
    session = FakeSession(status=online_status())
    _, port = make_server(sessions=[session])

    assert post_command(port, {"index": 0, "action": "resume"})[0] == 200
    assert post_command(port, {"index": 0, "action": "stop"})[0] == 200
    # 灯光状态为 on → light_toggle 应当下发关灯
    assert post_command(port, {"index": 0, "action": "light_toggle"})[0] == 200
    assert post_command(port, {"index": 0, "action": "light_on"})[0] == 200
    assert post_command(port, {"index": 0, "action": "speed", "value": "9"})[0] == 200
    assert post_command(port, {"index": 0, "action": "speed", "value": "0"})[0] == 200

    assert ("resume",) in session.calls
    assert ("stop",) in session.calls
    assert ("light", False) in session.calls, "灯已开时 light_toggle 应为关灯"
    assert ("light", True) in session.calls
    assert ("speed", 4) in session.calls, "档位 9 必须夹到 4"
    assert ("speed", 1) in session.calls, "档位 0 必须夹到 1"


def test_api_info_把令牌与局域网地址给到网页():
    """契约：`GET /api/info` 返回访问令牌与可供其它设备访问的地址。

    这条守的是一个**安卓专属的可用性缺陷**：令牌只在打开页面那一刻出现在
    URL 里，随后被前端 `history.replaceState` 抹掉（免得截图/分享时泄露），
    而安卓版没有终端、看不到启动时打印的地址 —— 于是用户在平板上能用，
    却拿不到令牌、没法在电脑上打开。桌面版有「网页信息」对话框，安卓版只能靠网页自己给。
    """
    token = "0123456789abcdef"
    sessions = [FakeSession(status=online_status())]
    holder = list(sessions)

    # 用真实的 WebHost.info（它内部会枚举本机网卡；测试环境的网络护栏可能
    # 拦住那一步，所以只断言"令牌一定给出来了"这个关键点）
    from app.config import AppConfig
    from app.web.host import WebHost

    config = AppConfig()
    config.web_token = token
    config.persist = False
    host = WebHost(config, lambda: holder)

    server = WebServer(
        get_sessions=lambda: holder,
        port=0,
        token=token,
        host="127.0.0.1",
        info_fn=host.info,
    )
    assert server.start() is True
    try:
        port = server._httpd.server_address[1]

        # 未授权时不给（令牌本身就是凭据，不能白送）
        status, _, _ = request(port, "/api/info")
        assert status == 401, "没令牌不该拿到令牌"

        status, headers, body = request(port, f"/api/info?token={token}")
        assert status == 200, body[:300]
        assert headers["content-type"].startswith("application/json")
        payload = json.loads(body.decode("utf-8"))

        assert payload["supported"] is True
        assert payload["token"] == token, "必须把令牌给出来，否则没终端就抄不到"
        assert payload["port"] == port, (
            "端口必须报实际绑定的那个 —— port=0 时 WebServer.port 还是 0，"
            "用户按它拼地址会打不开"
        )
        assert payload["local_urls"], "至少要给出本机回环地址"
        assert all(str(port) in url for url in payload["urls"]), (
            "地址里的端口必须是实际端口，否则用户在别的设备上打不开"
        )
        assert all(token in url for url in payload["urls"]), (
            "给出的地址必须自带令牌 —— 用户是直接复制的，不该再手输"
        )
    finally:
        server.stop()


def test_api_info_没有实现时返回501(make_server):
    """契约：宿主没注入 `info_fn` 时返回 501 并说明，而不是 500。"""
    _, port = make_server()
    status, _, body = request(port, "/api/info")
    assert status == 501
    payload = json.loads(body.decode("utf-8"))
    assert payload["supported"] is False


def test_command_can_control为假时返回400(make_server):
    """契约：遥测未连接（can_control=False）时拒绝下发，返回 400 且不调用控制方法。"""
    session = FakeSession(status=online_status(), can_control=False)
    _, port = make_server(sessions=[session])
    status, _, body = post_command(port, {"index": 0, "action": "pause"})
    assert status == 400
    payload = json.loads(body.decode("utf-8"))
    assert payload["ok"] is False
    assert "遥测" in payload["detail"]
    assert session.calls == []


def test_签名挡住的只是print段命令_can_control仍为真(make_server):
    """契约：固件要求签名时，`can_control` 必须仍是 True，只有 `print` 段命令被挡。

    这条守的是一个**误导性提示**的根因：网页端只要拿到非空的
    `controls_blocked_reason` 就会在状态条上显示提示。若把签名要求也算进
    `can_control`，灯控（走 `system` 段、**实测可用**）也会被一起判成不可用，
    用户就会看到"控制不可用"却发现灯明明能开关 —— 提示与事实矛盾。

    所以这里断言：
    * 遥测在线 + 需要签名 -> `can_control` 为 True（连接是好的）
    * `controls_blocked_reason` 非空（界面据此提示"部分命令被挡"）
    * 但灯控**不在**被挡之列
    """
    status = online_status()
    # `fun` 的 bit 0x20000000 置位 = 固件要求 MQTT 命令签名（未开开发者模式）
    status.apply_report({"print": {"fun": "100d122002fbd"}})
    assert status.needs_mqtt_signature is True, "前提没造出来"
    session = FakeSession(status=status)
    # 真实会话由 command_blocked 按命令判定；这里对齐同样的语义
    session.command_blocked = lambda cmd: (
        "固件要求签名" if cmd in ("pause", "resume", "stop", "speed") else ""
    )
    session.controls_blocked_reason = "固件要求签名"

    _, port = make_server(sessions=[session])
    status_code, _, body = request(port, "/api/printers")
    assert status_code == 200
    item = json.loads(body.decode("utf-8"))["printers"][0]

    assert item["can_control"] is True, (
        "需要签名不等于连不上：把它算进 can_control 会让灯控一起被锁"
    )
    assert item["controls_blocked_reason"], "必须把原因带给界面，否则按钮灰了却不说为什么"

    # 灯控照常可用
    code, _, body = post_command(port, {"index": 0, "action": "light_on"})
    assert code == 200, "灯控走 system 段，不该被签名要求挡住"
    assert ("light", True) in session.calls

    # 暂停被挡，且原因回给用户
    code, _, body = post_command(port, {"index": 0, "action": "pause"})
    assert code == 400
    assert "签名" in json.loads(body.decode("utf-8"))["detail"]


def test_command_未知动作返回400(make_server):
    """契约：未知 action 返回 400 且说明是未知指令。"""
    session = FakeSession(status=online_status())
    _, port = make_server(sessions=[session])
    status, _, body = post_command(port, {"index": 0, "action": "self_destruct"})
    assert status == 400
    payload = json.loads(body.decode("utf-8"))
    assert payload["ok"] is False
    assert "未知指令" in payload["detail"]


def test_command_索引越界返回400(make_server):
    """契约：index 超出范围返回 400「没有这台打印机」，不崩溃。"""
    session = FakeSession(status=online_status())
    _, port = make_server(sessions=[session])
    status, _, body = post_command(port, {"index": 7, "action": "pause"})
    assert status == 400
    assert "没有这台打印机" in json.loads(body.decode("utf-8"))["detail"]
    assert session.calls == []


def test_command_非法JSON返回400(make_server):
    """契约：请求体不是合法 JSON 时返回 400 + {"ok":false}，不抛异常。"""
    session = FakeSession(status=online_status())
    _, port = make_server(sessions=[session])
    status, _, body = post_command(port, None, raw=b"{not json")
    assert status == 400
    payload = json.loads(body.decode("utf-8"))
    assert payload["ok"] is False
    assert session.calls == []


def test_command_无令牌返回401(make_server):
    """契约：POST /api/command 同样受令牌保护，无令牌返回 401 且不下发指令。"""
    session = FakeSession(status=online_status())
    _, port = make_server(sessions=[session], token=TOKEN)
    status, _, _ = post_command(port, {"index": 0, "action": "pause"})
    assert status == 401
    status, _, _ = post_command(port, {"index": 0, "action": "pause"}, token=TOKEN)
    assert status == 200
    assert session.calls == [("pause",)]


def test_command_错误路径返回404(plain_server):
    """契约：POST 到未注册路径返回 404。"""
    _, port = plain_server
    status, _, _ = request(port, "/api/nope", method="POST", body=b"{}")
    assert status == 404


def test_command_控制失败时返回400(make_server):
    """契约：session 返回 False 时接口返回 400（表示未成功下发）。"""
    session = FakeSession(status=online_status(), control_ok=False)
    _, port = make_server(sessions=[session])
    status, _, body = post_command(port, {"index": 0, "action": "pause"})
    assert status == 400
    assert json.loads(body.decode("utf-8"))["ok"] is False


# ------------------------------------------------------------------ 单帧快照
def test_single_frame_无帧返回503(make_server):
    """契约：会话还没有任何画面时 /api/frame/0 返回 503。"""
    session = FakeSession(status=PrinterStatus(), frame=None)
    _, port = make_server(sessions=[session])
    status, _, body = request(port, "/api/frame/0")
    assert status == 503
    assert body == b"no frame"


def test_single_frame_有帧返回JPEG(make_server):
    """契约：有画面时 /api/frame/0 返回 200 + image/jpeg + 原始 JPEG 字节。"""
    session = FakeSession(status=PrinterStatus(camera_online=True), frame=JPEG)
    _, port = make_server(sessions=[session])
    status, headers, body = request(port, "/api/frame/0")
    assert status == 200
    assert headers["content-type"] == "image/jpeg"
    assert body.startswith(b"\xff\xd8")
    assert body == JPEG


def test_single_frame_索引非法与越界(make_server):
    """契约：索引不是数字返回 400，越界返回 404。"""
    session = FakeSession(status=PrinterStatus(), frame=None)
    _, port = make_server(sessions=[session])
    assert request(port, "/api/frame/abc")[0] == 400
    assert request(port, "/api/frame/3")[0] == 404


def test_stream_响应头为multipart(make_server):
    """契约：/stream/<i> 使用 multipart/x-mixed-replace，浏览器 <img> 可直接播放。"""
    session = FakeSession(status=PrinterStatus(camera_online=True), frame=JPEG)
    server, port = make_server(sessions=[session])
    # 直接塞进缓存，避免等待后台转码线程
    with server.cache._lock:
        server.cache._frames[0] = (1, JPEG)

    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.settimeout(5)
    try:
        sock.sendall(b"GET /stream/0 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        data = b""
        # 记录头（含 --bmframe）会先到，JPEG 负载随后；读到整帧再停
        while JPEG not in data and len(data) < 200_000:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()

    assert b"multipart/x-mixed-replace" in data
    assert b"boundary=bmframe" in data
    assert JPEG in data


# ------------------------------------------------------------------ 生命周期
def test_首次start即可用():
    """契约（回归）：全新构造的 WebServer 第一次 start() 就应成功、立刻能响应 /health，
    且不能因为「对未启动的转码线程 join()」而抛 RuntimeError。"""
    server = WebServer(
        get_sessions=lambda: [], port=0, token="", fps=4.0, max_width=720, host="127.0.0.1"
    )
    try:
        assert server.start() is True
        assert server.running is True
        port = server._httpd.server_address[1]
        server.port = port
        assert request(port, "/health")[0] == 200
        assert server.cache.is_alive(), "首次 start() 后转码线程应当在运行"
    finally:
        server.stop()


def test_stop未启动的服务不抛异常():
    """契约（回归）：从未 start() 过的 WebServer 调用 stop() 应当安全返回，
    不得因为 join 未启动的线程而抛 RuntimeError。"""
    server = WebServer(
        get_sessions=lambda: [], port=0, token="", fps=4.0, max_width=720, host="127.0.0.1"
    )
    server.stop()  # 不应抛异常
    assert server.running is False
    server.stop()  # 重复 stop 也应安全
    # 之后仍然可以正常启动
    try:
        assert server.start() is True
        server.port = server._httpd.server_address[1]
        status, _, body = request(server.port, "/health")
        assert status == 200 and body == b"ok"
    finally:
        server.stop()


def test_stop之后可以再次start():
    """契约：stop() 释放端口与转码线程后，start() 能重新拉起服务并继续响应 /health。"""
    server = WebServer(
        get_sessions=lambda: [], port=0, token="", fps=4.0, max_width=720, host="127.0.0.1"
    )
    try:
        assert server.start() is True
        assert server.running is True
        port = server._httpd.server_address[1]
        server.port = port
        assert request(port, "/health")[0] == 200

        server.stop()
        assert server.running is False
        assert server._httpd is None

        assert server.start() is True, "stop() 之后必须能再次 start()"
        assert server.running is True
        port2 = server._httpd.server_address[1]
        server.port = port2
        status, _, body = request(port2, "/health")
        assert status == 200 and body == b"ok"
    finally:
        server.stop()


def test_重启保留max_width():
    """契约：start() 重建 WebFrameCache 时必须原样带上用户设置的 max_width，
    否则网页画面宽度会在重启后静默退回默认 720。"""
    server = WebServer(
        get_sessions=lambda: [], port=0, token="", fps=4.0, max_width=480, host="127.0.0.1"
    )
    try:
        assert server.cache._max_width == 480, "构造后缓存应记住 480"
        assert server.start() is True
        assert server.cache._max_width == 480, "start() 重建缓存后仍应是 480（不得退回 720）"
        server.port = server._httpd.server_address[1]
        server.stop()
        assert server.start() is True
        assert server.cache._max_width == 480, "二次 start() 之后仍应是 480"
    finally:
        server.stop()


def test_start重复调用是幂等的():
    """契约：已在运行时再次 start() 返回 True 且不重复绑定端口。"""
    server = WebServer(
        get_sessions=lambda: [], port=0, token="", fps=4.0, max_width=720, host="127.0.0.1"
    )
    try:
        assert server.start() is True
        port = server._httpd.server_address[1]
        server.port = port
        assert server.start() is True
        assert server._httpd.server_address[1] == port
        assert request(port, "/health")[0] == 200
    finally:
        server.stop()


# ------------------------------------------------------------------ 对外地址
def test_urls包含回环地址与令牌(token_server):
    """契约：urls() 至少给出 http://127.0.0.1:<port> 形式的地址，带令牌时拼接 ?token=。"""
    server, port = token_server
    urls = server.urls()
    loopback = [url for url in urls if url.startswith("http://127.0.0.1:")]
    assert loopback, f"urls() 必须包含回环地址，实际为 {urls}"
    assert loopback[0] == f"http://127.0.0.1:{port}/?token={TOKEN}"


def test_urls不带令牌时无查询串(plain_server):
    """契约：未配置令牌时地址不带 ?token=。"""
    server, port = plain_server
    loopback = [url for url in server.urls() if url.startswith("http://127.0.0.1:")]
    assert loopback[0] == f"http://127.0.0.1:{port}/"
    assert "?" not in loopback[0]
