"""网页端设备管理与设置的契约。

这些能力是为了让**网页成为各平台的统一界面**而加的：
桌面端原有「自动搜索 / 添加打印机 / 设置」对话框，而安卓版没有桌面界面，
网页是唯一入口 —— 没有这些，平板用户根本没法把打印机加进来。

服务端只暴露回调（`discover_fn` / `add_printer_fn` / `manage_printer_fn` /
`get_settings_fn` / `update_settings_fn`），真实实现由宿主提供
（`app/web/host.py` 是共享实现）。因此这里分两层测：

* **HTTP 层**：路由、鉴权、参数校验、不支持时的提示（注入假回调，不碰真设备）；
* **宿主层**：`WebHost` 的增删改查与配置落盘（用临时配置目录）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from app.bambu.models import PrinterInfo, PrinterModel, PrinterStatus
from app.config import AppConfig
from app.web.host import SETTINGS_SPEC, WebHost
from app.web.server import WebServer


class FakeSession:
    """网页服务真正会调用到的最小会话替身。"""

    def __init__(self, ip: str = "192.168.1.50", name: str = "测试机") -> None:
        self.info = PrinterInfo(ip=ip, name=name, model=PrinterModel.P1S)
        self.capabilities = PrinterModel.P1S.capabilities
        self.controls_blocked_reason = ""
        self.can_control = True
        self.video_backend = "TCP6000"
        self.camera_fps = 1.0
        self.restarts = 0
        self.stopped = False

    def snapshot(self):
        return PrinterStatus(mqtt_online=True, progress=47)

    def latest_frame(self):
        return 0, None

    def restart(self) -> None:  # noqa: D102
        self.restarts += 1

    def stop(self) -> None:  # noqa: D102
        self.stopped = True

    def set_max_fps(self, fps: float) -> None:  # noqa: D102
        self.fps = fps


@pytest.fixture()
def web_server(isolated_config_dir):
    """起一个带假回调的真实 HTTP 服务（端口 0 让系统分配）。"""
    calls: list[tuple] = []
    sessions = [FakeSession()]

    def discover():
        calls.append(("discover",))
        return [
            {"ip": "192.168.1.50", "serial": "01P", "name": "测试机", "model": "P1S"},
            {"ip": "192.168.1.77", "serial": "20P", "name": "新机", "model": "X2D"},
        ]

    def add_printer(name="", ip="", access_code="", model_label="", serial=""):
        calls.append(("add", name, ip, access_code, model_label, serial))
        return {"ok": True, "detail": "已添加"}

    def manage_printer(index=-1, action="", name="", access_code=""):
        calls.append(("manage", index, action, name, access_code))
        if action == "boom":
            return {"ok": False, "detail": "未知操作"}
        return {"ok": True, "detail": f"{action} 完成"}

    def get_settings():
        calls.append(("settings_get",))
        return {"supported": True, "max_fps": 10.0, "web_fps": 4.0}

    def update_settings(body):
        calls.append(("settings_set", body))
        return {"ok": True, "detail": "已保存"}

    server = WebServer(
        get_sessions=lambda: sessions,
        port=0,
        host="127.0.0.1",
        token="secret",
        discover_fn=discover,
        add_printer_fn=add_printer,
        manage_printer_fn=manage_printer,
        get_settings_fn=get_settings,
        update_settings_fn=update_settings,
    )
    assert server.start()
    port = server._httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        yield base, calls, sessions
    finally:
        server.stop()


def _get(base: str, path: str):
    with urllib.request.urlopen(f"{base}{path}?token=secret", timeout=10) as response:
        return response.status, json.loads(response.read())


def _post(base: str, path: str, payload: dict, token: str = "secret"):
    request = urllib.request.Request(
        f"{base}{path}?token={token}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# --------------------------------------------------------------------------- 发现


def test_自动搜索返回结果并标记已知设备(web_server):
    """契约：`/api/discover` 返回搜索结果，并标出哪些已经在监控墙上。"""
    base, calls, _sessions = web_server
    status, data = _get(base, "/api/discover")
    assert status == 200
    assert data["supported"] is True
    assert data["total"] == 2
    known = {item["ip"]: item["known"] for item in data["printers"]}
    assert known["192.168.1.50"] is True, "已配置的设备应标为 known"
    assert known["192.168.1.77"] is False
    assert ("discover",) in calls


def test_搜索不带令牌被拒(web_server):
    """契约：设备管理接口必须同样受令牌保护（否则同网段任何人都能改配置）。"""
    base, _calls, _sessions = web_server
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(f"{base}/api/discover", timeout=10)
    assert info.value.code == 401


# --------------------------------------------------------------------------- 添加


def test_添加设备会带上序列号传给宿主(web_server):
    """契约：`serial` 必须透传给宿主。

    这不是可选字段：**遥测靠序列号订阅 `device/<序列号>/report`**，
    缺了会出现「有画面但永远没有进度/温度」—— 实测踩过这个坑。
    """
    base, calls, _sessions = web_server
    status, data = _post(
        base,
        "/api/add_printer",
        {
            "name": "新机",
            "ip": "192.168.1.77",
            "access_code": "12345678",
            "model": "X2D",
            "serial": "20PABC",
        },
    )
    assert status == 200 and data["ok"] is True
    assert ("add", "新机", "192.168.1.77", "12345678", "X2D", "20PABC") in calls


def test_添加设备的非法JSON返回400(web_server):
    """契约：请求体不是合法 JSON 时给出 400，而不是 500 或静默成功。"""
    base, _calls, _sessions = web_server
    request = urllib.request.Request(
        f"{base}/api/add_printer?token=secret",
        data=b"{not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=10)
        raise AssertionError("应当返回 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


# --------------------------------------------------------------------------- 管理


@pytest.mark.parametrize("action", ["reconnect", "update", "remove"])
def test_设备管理三个动作都被转发(web_server, action):
    """契约：重连 / 编辑 / 删除都要转发给宿主，且带着索引。"""
    base, calls, _sessions = web_server
    status, data = _post(base, "/api/printers", {"index": 0, "action": action})
    assert status == 200 and data["ok"] is True
    assert ("manage", 0, action, "", "") in calls


def test_设备管理未知动作返回400(web_server):
    """契约：未知动作要明确拒绝（而不是静默当成成功）。"""
    base, _calls, _sessions = web_server
    status, data = _post(base, "/api/printers", {"index": 0, "action": "boom"})
    assert status == 400 and data["ok"] is False


# --------------------------------------------------------------------------- 设置


def test_设置可读可写(web_server):
    """契约：`GET /api/settings` 读、`POST /api/settings` 写。"""
    base, calls, _sessions = web_server
    status, data = _get(base, "/api/settings")
    assert status == 200 and data["supported"] is True
    status, data = _post(base, "/api/settings", {"web_fps": 8})
    assert status == 200 and data["ok"] is True
    assert ("settings_set", {"web_fps": 8}) in calls


def test_后端不支持时返回501而不是静默失败():
    """契约：宿主没提供能力时返回 501，前端据此提示「该运行方式不支持」。

    这样用户在只读场景下能得到明确说明，而不是点了没反应。
    """
    server = WebServer(get_sessions=lambda: [], port=0, host="127.0.0.1", token="t")
    assert server.start()
    base = f"http://127.0.0.1:{server._httpd.server_address[1]}"
    try:
        for path in ("/api/discover", "/api/settings"):
            try:
                urllib.request.urlopen(f"{base}{path}?token=t", timeout=10)
                raise AssertionError(f"{path} 应当返回 501")
            except urllib.error.HTTPError as exc:
                assert exc.code == 501, path
        status, data = _post(base, "/api/add_printer", {"ip": "1.2.3.4"}, token="t")
        assert status == 501 and data["ok"] is False
    finally:
        server.stop()


# --------------------------------------------------------------------------- 宿主


def test_宿主设置更新被钳制并落盘(isolated_config_dir):
    """契约：网页改设置要按范围钳制、写进配置并落盘。"""
    config = AppConfig()
    host = WebHost(config, lambda: [])
    result = host.update_settings({"max_fps": 999, "web_fps": 0.1, "不认识的字段": 1})
    assert result["ok"] is True
    assert config.max_fps == 30.0, "超上限应被钳到 30"
    assert config.web_fps == 0.5, "低于下限应被钳到 0.5"
    reloaded = AppConfig.load()
    assert reloaded.max_fps == 30.0, "设置必须落盘"


def test_宿主设置拒绝非法值(isolated_config_dir):
    """契约：值不是数字时明确报错，不能静默写进去。"""
    host = WebHost(AppConfig(), lambda: [])
    result = host.update_settings({"max_fps": "很快"})
    assert result["ok"] is False
    assert "不合法" in result["detail"]


def test_宿主设置规格与配置字段一致():
    """契约：`SETTINGS_SPEC` 里声明的字段必须真的存在于 AppConfig。

    否则网页上"保存成功"了，实际什么都没改。
    """
    for key in SETTINGS_SPEC:
        assert hasattr(AppConfig(), key), f"AppConfig 没有字段 {key}"


def test_宿主添加设备会建会话并落盘(isolated_config_dir, monkeypatch):
    """契约：通过网页添加设备后，配置里有它、会话列表里也有它。

    「立即建会话」是关键：否则用户加完要等服务重启才看得到，会以为没加上。
    """
    created: list = []

    class FakePrinterSession:
        def __init__(self, info):
            self.info = info
            created.append(info)

        def set_max_fps(self, fps):  # noqa: D102
            pass

        def start(self):  # noqa: D102
            self.started = True

        def restart(self):  # noqa: D102
            pass

        def stop(self):  # noqa: D102
            pass

    monkeypatch.setattr("app.bambu.printer.PrinterSession", FakePrinterSession)
    config = AppConfig()
    sessions: list = []
    host = WebHost(config, lambda: sessions)

    result = host.add_printer(name="新机", ip="192.168.1.77", access_code="12345678",
                              model_label="X2D", serial="20PABC")
    assert result["ok"] is True
    assert [p.ip for p in config.printers] == ["192.168.1.77"]
    assert config.printers[0].serial == "20PABC"
    assert len(sessions) == 1, "应当立即建立会话"
    assert AppConfig.load().printers[0].ip == "192.168.1.77", "必须落盘"


def test_宿主删除设备会同时移除配置与会话(isolated_config_dir):
    """契约：删除要同时清掉配置与会话，否则重启后又冒出来。"""
    config = AppConfig()
    config.printers.append(PrinterInfo(ip="192.168.1.50", name="测试机"))
    config.save()
    session = FakeSession()
    sessions = [session]
    host = WebHost(config, lambda: sessions)

    result = host.manage_printer(index=0, action="remove")
    assert result["ok"] is True
    assert session.stopped is True
    assert sessions == []
    assert config.printers == []
    assert AppConfig.load().printers == [], "配置里也必须删掉"


def test_宿主重连会重启会话(isolated_config_dir):
    """契约：重连动作要真的触发会话重启。"""
    session = FakeSession()
    host = WebHost(AppConfig(), lambda: [session])
    result = host.manage_printer(index=0, action="reconnect")
    assert result["ok"] is True
    assert session.restarts == 1


def test_宿主对越界索引给出可读提示(isolated_config_dir):
    """契约：索引越界要说明当前有几台，而不是抛 IndexError。"""
    host = WebHost(AppConfig(), lambda: [])
    result = host.manage_printer(index=3, action="remove")
    assert result["ok"] is False
    assert "共 0 台" in result["detail"]
