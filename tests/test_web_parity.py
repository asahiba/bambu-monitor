"""网页端补齐的「桌面版能力」接口：全部连接/断开、画面顺序。

## 为什么单独立一条

用户的要求是「网页要拥有本地端的全部功能」。桌面版工具栏有
「▶ 全部连接」「■ 全部断开」，右键菜单有「上移 / 下移 / 移到最前 / 移到最后」，
而网页端过去只能一台一台点重连、只能改画面大小。这两个能力现在走
``POST /api/sessions`` 与 ``POST /api/layout {"action": …}``。

契约要点：

* **只动监控连接与显示顺序**，不下发任何控制指令（打印机照常打印）；
* 顺序要**同时**改当前会话列表（界面立刻变）与配置（重启后还是这个顺序）；
* 已经排到头的再上移、排到尾的再下移都要是安全的空操作，不能报错也不能乱序。
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

TOKEN = "parity"

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
    """只需要能被 start/stop、有 info 的最小替身。"""

    def __init__(self, ip: str, name: str) -> None:
        self.info = PrinterInfo(ip=ip, name=name, model=PrinterModel.P1S, access_code="12345678")
        self.capabilities = self.info.model.capabilities
        self.can_control = True
        self.controls_blocked_reason = ""
        self.controls_blocked_short = ""
        self.video_unavailable_reason = ""
        self.video_backend = "TCP6000"
        self.camera_fps = 1.0
        self.mqtt_auth_error = False
        self.last_camera_state = "streaming"
        self.last_camera_detail = ""
        self.running = False
        self.starts = 0
        self.stops = 0

    def snapshot(self) -> PrinterStatus:
        return PrinterStatus(mqtt_online=self.running, camera_online=self.running)

    def latest_frame(self):
        return 0, None

    def start(self) -> None:
        self.running = True
        self.starts += 1

    def stop(self) -> None:
        self.running = False
        self.stops += 1

    def restart(self) -> None:
        self.stop()
        self.start()


@pytest.fixture()
def environment(isolated_config_dir):
    """三台设备 + 一个真服务；列出当前顺序供断言。"""

    def _make(count: int = 3):
        sessions = [FakeSession(f"192.168.1.{index + 10}", f"机器{index + 1}") for index in range(count)]
        config = AppConfig()
        config.persist = True
        config.printers = [session.info for session in sessions]
        host = WebHost(config, lambda: sessions)
        web = WebServer(
            get_sessions=lambda: sessions,
            port=0,
            token=TOKEN,
            host="127.0.0.1",
            layout_fn=host.set_tile_span,
            reorder_fn=host.reorder,
            sessions_action_fn=host.sessions_action,
        )
        assert web.start() is True
        web.port = web._httpd.server_address[1]  # noqa: SLF001
        return sessions, config, host, web

    created: list[WebServer] = []
    made: list = []

    def factory(count: int = 3):
        result = _make(count)
        created.append(result[3])
        made.append(result)
        return result

    yield factory

    for web in created:
        web.stop()


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


def _order(sessions: list) -> list[str]:
    return [session.info.name for session in sessions]


# ------------------------------------------------------------------ 全部连接/断开
def test_全部断开再全部连接(environment):
    """契约：`connect_all` / `disconnect_all` 要真的作用于每一台。"""
    sessions, _config, _host, web = environment()
    base = f"http://127.0.0.1:{web.port}"

    status, result = _post(base, f"/api/sessions?token={TOKEN}", {"action": "connect_all"})
    assert status == 200 and result["ok"] is True
    assert all(session.running for session in sessions), "没有真的连接"
    assert "3" in result["detail"]

    status, result = _post(base, f"/api/sessions?token={TOKEN}", {"action": "disconnect_all"})
    assert status == 200 and result["ok"] is True
    assert not any(session.running for session in sessions), "没有真的断开"
    assert all(session.stops == 1 for session in sessions)


def test_未知操作返回400(environment):
    _sessions, _config, _host, web = environment()
    status, result = _post(
        f"http://127.0.0.1:{web.port}",
        f"/api/sessions?token={TOKEN}",
        {"action": "reboot_everything"},
    )
    assert status == 400 and result["ok"] is False
    assert "未知操作" in result["detail"]


# ------------------------------------------------------------------ 画面顺序
def test_上移一台设备(environment):
    """契约：顺序要**同时**改会话列表与配置（否则重启就回去了）。"""
    sessions, config, _host, web = environment()
    base = f"http://127.0.0.1:{web.port}"

    status, result = _post(base, f"/api/layout?token={TOKEN}", {"index": 1, "action": "up"})
    assert status == 200 and result["ok"] is True
    assert _order(sessions) == ["机器2", "机器1", "机器3"]
    assert [item.name for item in config.printers] == ["机器2", "机器1", "机器3"], "配置顺序没同步"
    assert result["order"] == ["192.168.1.11", "192.168.1.10", "192.168.1.12"]


def test_下移移到最前移到最后(environment):
    sessions, _config, _host, web = environment()
    base = f"http://127.0.0.1:{web.port}"

    _post(base, f"/api/layout?token={TOKEN}", {"index": 0, "action": "bottom"})
    assert _order(sessions) == ["机器2", "机器3", "机器1"]

    _post(base, f"/api/layout?token={TOKEN}", {"index": 2, "action": "top"})
    assert _order(sessions) == ["机器1", "机器2", "机器3"]

    _post(base, f"/api/layout?token={TOKEN}", {"index": 0, "action": "down"})
    assert _order(sessions) == ["机器2", "机器1", "机器3"]


def test_排到头再移动是安全的空操作(environment):
    """契约：第一个再上移、最后一个再下移都不能乱序，也不该报错。"""
    sessions, _config, _host, web = environment()
    base = f"http://127.0.0.1:{web.port}"

    status, result = _post(base, f"/api/layout?token={TOKEN}", {"index": 0, "action": "up"})
    assert status == 200 and result["ok"] is True
    assert _order(sessions) == ["机器1", "机器2", "机器3"]

    status, result = _post(base, f"/api/layout?token={TOKEN}", {"index": 2, "action": "down"})
    assert status == 200 and result["ok"] is True
    assert _order(sessions) == ["机器1", "机器2", "机器3"]


def test_未知方向与越界序号返回400(environment):
    _sessions, _config, _host, web = environment()
    base = f"http://127.0.0.1:{web.port}"
    status, result = _post(base, f"/api/layout?token={TOKEN}", {"index": 0, "action": "sideways"})
    assert status == 400 and "未知方向" in result["detail"]
    status, result = _post(base, f"/api/layout?token={TOKEN}", {"index": 9, "action": "up"})
    assert status == 400 and "没有第 10 台" in result["detail"]


def test_还是能改画面大小(environment):
    """契约：加了 action 之后，原来的 `{"span": 2}` 用法不能坏。"""
    sessions, _config, _host, web = environment()
    status, result = _post(
        f"http://127.0.0.1:{web.port}",
        f"/api/layout?token={TOKEN}",
        {"index": 0, "span": 2},
    )
    assert status == 200 and result["ok"] is True
    assert sessions[0].info.tile_span == 2
