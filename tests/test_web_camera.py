"""网页端的「刷新画面 / 切换摄像头」接口（`/api/camera`、`/api/frame?cam=`）。

## 为什么要有这两个能力

桌面版右键菜单一直有「重新连接」，而网页端过去连「刷新一下画面」都做不到：
摄像头刚插上、或者之前返回 502 的那一路刚起来时，用户在网页上没有任何办法
让它重新试一次。多摄像头的机器（Voron 常见喷嘴 + 舱内两路）也没有切换入口。

现在：

* ``POST /api/camera {"action":"refresh"}`` —— 重新发现摄像头 + 丢掉缓存帧，立刻生效；
* ``POST /api/camera {"action":"select","camera":N}`` —— 改看第 N 路，并写进配置；
* ``GET /api/frame/<i>?cam=N`` —— 取指定那一路；不给就用该设备当前选的那一路。

全部只碰画面，不下发任何会动机器的指令。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from app.bambu.models import PrinterInfo, PrinterModel, PrinterStatus  # noqa: E402
from app.config import AppConfig  # noqa: E402
from app.web.host import WebHost  # noqa: E402
from app.web.server import WebServer  # noqa: E402

TOKEN = "cam"
#: 两路画面用**不同的字节**，这样才能断言"取到的确实是第二路"
JPEG_A = b"\xff\xd8\xff\xe0" + b"A" * 512 + b"\xff\xd9"
JPEG_B = b"\xff\xd8\xff\xe0" + b"B" * 700 + b"\xff\xd9"

_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_BIND = socket.socket.bind
_REAL_LISTEN = socket.socket.listen
_REAL_CREATE_CONNECTION = socket.create_connection


def _is_loopback(address) -> bool:
    host = str(address[0] if isinstance(address, tuple) and address else "")
    return host in ("", "localhost", "::1") or host.startswith("127.")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """只放行回环（与 test_web_server.py 同一套窄守卫）。"""

    def tcp_guard(original, name):
        def wrapper(self, address, *args, **kwargs):
            if getattr(self, "type", None) == socket.SOCK_STREAM and not _is_loopback(address):
                raise RuntimeError(f"测试禁止连接非回环 TCP 地址（{name} {address!r}）")
            return original(self, address, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(socket.socket, "connect", tcp_guard(_REAL_CONNECT, "connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", tcp_guard(_REAL_CONNECT_EX, "connect_ex"))
    monkeypatch.setattr(socket.socket, "bind", _REAL_BIND)
    monkeypatch.setattr(socket.socket, "listen", _REAL_LISTEN)
    monkeypatch.setattr(socket, "create_connection", _REAL_CREATE_CONNECTION)


class FakeSession:
    """多路画面的会话替身：`cameras()` + `latest_frame(camera)`。"""

    def __init__(self, cameras: int = 2) -> None:
        self.info = PrinterInfo(
            ip="192.168.31.55",
            name="Voron",
            family="moonraker",
            port=7125,
            model=PrinterModel.UNKNOWN,
        )
        self.capabilities = self.info.model.capabilities
        self.can_control = True
        self.controls_blocked_reason = ""
        self.controls_blocked_short = ""
        self.video_unavailable_reason = ""
        self.video_backend = "快照"
        self.camera_fps = 2.0
        self.mqtt_auth_error = False
        self.last_camera_state = "streaming"
        self.last_camera_detail = "画面正常"
        self.refreshed = 0
        self._frames = {0: JPEG_A, 1: JPEG_B}
        self._cameras = cameras
        self.started = False

    def snapshot(self) -> PrinterStatus:
        return PrinterStatus(mqtt_online=True, camera_online=True)

    def cameras(self) -> list[dict]:
        items = []
        for index in range(self._cameras):
            items.append(
                {
                    "index": index,
                    "name": "Printer" if index == 0 else "nozzle",
                    "location": "printer" if index == 0 else "nozzle",
                    "available": index in self._frames,
                    "detail": "",
                    "url": f"http://192.168.31.55/webcam{'' if index == 0 else index + 1}/?action=snapshot",
                    "stream_url": "",
                }
            )
        return items

    def latest_frame(self, camera: int = 0):
        return 1, self._frames.get(int(camera))

    def refresh_cameras(self) -> list[dict]:
        self.refreshed += 1
        return self.cameras()

    def set_max_fps(self, fps: float) -> None:
        pass

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def restart(self) -> None:
        pass


@pytest.fixture()
def server(isolated_config_dir):
    """起一个真服务（回环 + 随机端口），返回 ``(WebServer, WebHost)``。"""
    created: list[WebServer] = []

    def _make(sessions: list, config: AppConfig | None = None):
        config = config or AppConfig()
        config.persist = getattr(config, "persist", False)
        config.printers = [session.info for session in sessions]
        host = WebHost(config, lambda: list(sessions))
        web = WebServer(
            get_sessions=lambda: list(sessions),
            port=0,
            token=TOKEN,
            host="127.0.0.1",
            camera_action_fn=host.camera_action,
            manage_printer_fn=host.manage_printer,
        )
        assert web.start() is True
        web.port = web._httpd.server_address[1]  # noqa: SLF001
        created.append(web)
        return web, host

    yield _make

    for web in created:
        web.stop()


def _get(base: str, path: str) -> tuple[int, bytes]:
    request = urllib.request.Request(f"{base}{path}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post(base: str, path: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _base(web: WebServer) -> str:
    return f"http://127.0.0.1:{web.port}"


# ------------------------------------------------------------------ 载荷
def test_每台设备都带摄像头列表与当前选中(server):
    """契约：`/api/printers` 要给出可选画面与当前那一路，前端才知道怎么渲染切换。"""
    session = FakeSession(cameras=2)
    web, _host = server([session])
    status, body = _get(_base(web), f"/api/printers?token={TOKEN}")
    assert status == 200
    item = json.loads(body)["printers"][0]
    assert [cam["name"] for cam in item["cameras"]] == ["Printer", "nozzle"]
    assert item["camera"] == 0


def test_只有一路时列表只有一条(server):
    """契约：单摄像头机器也返回一条 —— 前端据此决定**不显示**切换入口。"""
    web, _host = server([FakeSession(cameras=1)])
    _status, body = _get(_base(web), f"/api/printers?token={TOKEN}")
    assert len(json.loads(body)["printers"][0]["cameras"]) == 1


# ------------------------------------------------------------------ 取帧
def test_取指定那一路的画面(server):
    """契约：`?cam=N` 取第 N 路；不给就用该设备当前选中的那一路。"""
    session = FakeSession(cameras=2)
    web, _host = server([session])

    _status, body = _get(_base(web), f"/api/frame/0?token={TOKEN}")
    assert body == JPEG_A, "默认应当是第一路"

    _status, body = _get(_base(web), f"/api/frame/0?cam=1&token={TOKEN}")
    assert body == JPEG_B, "cam=1 应当是第二路"

    # 选了第二路之后，不带 cam 也取第二路
    session.info.camera_index = 1
    _status, body = _get(_base(web), f"/api/frame/0?token={TOKEN}")
    assert body == JPEG_B


def test_cam参数写坏了不会500(server):
    """契约：`?cam=abc` 这类脏参数要退回默认，而不是报错。"""
    web, _host = server([FakeSession(cameras=2)])
    for bad in ("abc", "-1", "", "9"):
        status, body = _get(_base(web), f"/api/frame/0?cam={bad}&token={TOKEN}")
        assert status == 200, (bad, status)
        assert body.startswith(b"\xff\xd8")


# ------------------------------------------------------------------ 刷新 / 切换
def test_刷新画面会重新发现摄像头(server):
    """契约：`refresh` 要真的调用会话的 `refresh_cameras()`（用户点的「刷新视频流」）。"""
    session = FakeSession(cameras=2)
    web, _host = server([session])
    status, result = _post(_base(web), f"/api/camera?token={TOKEN}", {"index": 0, "action": "refresh"})
    assert status == 200 and result["ok"] is True
    assert session.refreshed == 1, "没有真的重新发现摄像头"
    assert len(result["cameras"]) == 2
    assert "画面通道" in result["detail"] or "重新" in result["detail"]


def test_刷新对没有该能力的会话也不报错(server):
    """契约：拓竹那族没有 `refresh_cameras()`，刷新应当仍然成功（请求重新取帧）。"""

    class Plain(FakeSession):
        refresh_cameras = None  # type: ignore[assignment]

    session = Plain(cameras=1)
    session.__dict__.pop("refresh_cameras", None)
    web, _host = server([session])
    status, result = _post(_base(web), f"/api/camera?token={TOKEN}", {"index": 0, "action": "refresh"})
    assert status == 200 and result["ok"] is True


def test_切换摄像头会写进配置(server, isolated_config_dir):
    """契约：`select` 要记住用户选了哪一路（重启后还得是那一路）。"""
    session = FakeSession(cameras=2)
    config = AppConfig()
    config.persist = True
    config.printers = [session.info]
    web, _host = server([session], config)

    status, result = _post(
        _base(web), f"/api/camera?token={TOKEN}", {"index": 0, "action": "select", "camera": 1}
    )
    assert status == 200 and result["ok"] is True
    assert session.info.camera_index == 1
    assert result["camera"] == 1
    reloaded = json.loads((isolated_config_dir / "config.json").read_text(encoding="utf-8"))
    assert reloaded["printers"][0]["camera_index"] == 1, "切换没有被保存"


def test_越界的摄像头序号会夹回0(server):
    """契约：负数序号夹回第一路，避免出现一个永远取不到帧的选择。"""
    session = FakeSession(cameras=2)
    web, _host = server([session])
    status, result = _post(
        _base(web), f"/api/camera?token={TOKEN}", {"index": 0, "action": "select", "camera": -3}
    )
    assert status == 200, result
    assert session.info.camera_index == 0


def test_未知操作返回400(server):
    web, _host = server([FakeSession()])
    status, result = _post(_base(web), f"/api/camera?token={TOKEN}", {"index": 0, "action": "boom"})
    assert status == 400 and result["ok"] is False
    assert "未知操作" in result["detail"]


def test_设备序号越界返回400(server):
    web, _host = server([FakeSession()])
    status, result = _post(_base(web), f"/api/camera?token={TOKEN}", {"index": 5, "action": "refresh"})
    assert status == 400
    assert "没有第 6 台" in result["detail"]


def test_没有令牌被拒(server):
    """契约：画面接口同样要令牌保护（否则同网段谁都能改你看哪一路）。"""
    web, _host = server([FakeSession()])
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(f"{_base(web)}/api/frame/0", timeout=10)
    assert info.value.code == 401
