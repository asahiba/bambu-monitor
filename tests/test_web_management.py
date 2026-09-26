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
from pathlib import Path

import pytest

from app import config
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

    def add_printer(
        name="",
        ip="",
        access_code="",
        model_label="",
        serial="",
        family="",
        port=0,
        api_key="",
        camera_url="",
        light_on_gcode="",
        light_off_gcode="",
    ):
        # ⚠️ 这里必须与 `WebHost.add_printer` 的签名**逐项一致**：
        # 服务端是按关键字调用它的，签名漂了就会变成 400（踩过两次）
        calls.append(
            (
                "add",
                name,
                ip,
                access_code,
                model_label,
                serial,
                family,
                port,
                api_key,
                camera_url,
                light_on_gcode,
                light_off_gcode,
            )
        )
        return {"ok": True, "detail": "已添加"}

    def manage_printer(
        index=-1,
        action="",
        name="",
        access_code="",
        api_key="",
        port=0,
        light_on_gcode="",
        light_off_gcode="",
    ):
        calls.append(
            ("manage", index, action, name, access_code, api_key, port, light_on_gcode, light_off_gcode)
        )
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
    assert (
        "add", "新机", "192.168.1.77", "12345678", "X2D", "20PABC", "", 0, "", "", "", ""
    ) in calls


def test_添加设备会带上设备族与第三方凭据(web_server):
    """契约：`family` / `port` / `api_key` / `camera_url` 必须透传给宿主。

    Moonraker 这类第三方族**没有"访问代码"**：凭据叫 API Key、端口也不是 8883
    （默认 80）。这四个字段漏一个，网页/安卓上就加不对第三方设备。
    """
    base, calls, _sessions = web_server
    status, data = _post(
        base,
        "/api/add_printer",
        {
            "name": "Voron",
            "ip": "192.168.1.90",
            "family": "moonraker",
            "port": 7125,
            "api_key": "abc123",
            "camera_url": "http://192.168.1.90/webcam/?action=snapshot",
        },
    )
    assert status == 200 and data["ok"] is True
    assert (
        "add",
        "Voron",
        "192.168.1.90",
        "",
        "",
        "",
        "moonraker",
        7125,
        "abc123",
        "http://192.168.1.90/webcam/?action=snapshot",
        "",
        "",
    ) in calls


def test_灯光命令也透传给宿主(web_server):
    """契约：网页填的开/关灯 G-code 必须送到宿主，否则配置永远存不下来。

    Klipper 机器上"舱灯"没有统一做法（宏 / 输出引脚 / 某个风扇），所以由用户填命令；
    这两个键漏掉的表现是"填了没反应"—— 用户只会以为软件坏了。
    """
    base, calls, _sessions = web_server
    status, data = _post(
        base,
        "/api/add_printer",
        {
            "name": "Voron",
            "ip": "192.168.1.90",
            "family": "moonraker",
            "light_on_gcode": "LIGHT_ON",
            "light_off_gcode": "SET_PIN PIN=caselight VALUE=0",
        },
    )
    assert status == 200 and data["ok"] is True
    add_call = next(call for call in calls if call[0] == "add")
    assert add_call[10] == "LIGHT_ON"
    assert add_call[11] == "SET_PIN PIN=caselight VALUE=0"


def test_编辑设备也能改灯光命令(web_server):
    """契约：`action:"update"` 要能带上灯光命令。"""
    base, calls, _sessions = web_server
    status, data = _post(
        base,
        "/api/printers",
        {"index": 0, "action": "update", "light_on_gcode": "LIGHT_ON"},
    )
    assert status == 200 and data["ok"] is True
    manage_call = next(call for call in calls if call[0] == "manage")
    assert manage_call[7] == "LIGHT_ON"


def test_端口非法时按默认端口处理(web_server):
    """契约：端口写坏了（字母、越界）不能变成 500，按"用族默认端口"处理。"""
    base, calls, _sessions = web_server
    for bad in ("http", -1, 99999, None, ""):
        status, data = _post(
            base, "/api/add_printer", {"ip": "192.168.1.91", "port": bad, "access_code": "1"}
        )
        assert status == 200 and data["ok"] is True, f"port={bad!r} 应该被当成 0"
    assert all(call[7] == 0 for call in calls if call[0] == "add")


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
    assert ("manage", 0, action, "", "", "", 0, "", "") in calls


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


# --------------------------------------------------------------------------- 配置备份（跨版本）


class _FakeSessionFactory:
    """替身会话工厂：`import_config_text` 会用它重建会话列表。"""

    def __init__(self, monkeypatch):
        self.created: list = []
        self.sessions_created: list = []
        monkeypatch.setattr("app.bambu.printer.PrinterSession", self)

    def __call__(self, info):
        self.created.append(info)
        session = _StubSession(info)
        self.sessions_created.append(session)
        return session


class _StubSession:
    """替身会话本体（只实现宿主会用到的方法）。"""

    def __init__(self, info) -> None:
        self.info = info

    def set_max_fps(self, fps):  # noqa: D102
        pass

    def start(self):  # noqa: D102
        self.started = True

    def stop(self):  # noqa: D102
        self.stopped = True


def test_网页端导出配置返回可导入的内容(isolated_config_dir):
    """契约：网页/安卓端也能导出配置（以前只有桌面版有菜单项）。"""
    cfg = AppConfig()
    cfg.printers = [PrinterInfo(ip="192.168.1.50", name="测试机", access_code="12345678")]
    host = WebHost(cfg, lambda: [])

    result = host.export_config_text()
    assert result["ok"] is True
    assert result["printers"] == 1
    text = result["json"]
    assert "192.168.1.50" in text

    # 生成的内容必须能被「另一台机器」导入
    fresh = AppConfig()
    target = Path(config.config_dir()) / "from-web.json"
    target.write_text(text, encoding="utf-8")
    assert fresh.import_from(str(target)) is True
    assert fresh.printers[0].access_code == "12345678"


def test_网页端带口令导出可在任何版本导入(isolated_config_dir):
    """契约：网页端也能带口令导出 —— 安卓版就靠这条把配置搬到别的机器。"""
    cfg = AppConfig()
    cfg.printers = [PrinterInfo(ip="192.168.1.50", name="测试机", access_code="12345678")]
    host = WebHost(cfg, lambda: [])

    result = host.export_config_text("共享口令")
    assert result["ok"] is True and result["portable"] is True
    assert "12345678" not in result["json"], "带口令导出不得出现明文访问代码"

    fresh = AppConfig()
    target = Path(config.config_dir()) / "web-portable.json"
    target.write_text(result["json"], encoding="utf-8")
    assert fresh.import_from(str(target), "共享口令") is True
    assert fresh.printers[0].access_code == "12345678"


def test_网页端导入配置会替换设备并重建会话(isolated_config_dir, monkeypatch):
    """契约：导入 = 用文件里的设备替换当前配置，并立刻重建会话（不用重启服务）。"""
    factory = _FakeSessionFactory(monkeypatch)
    cfg = AppConfig()
    cfg.printers = [PrinterInfo(ip="192.168.1.50", name="旧机", access_code="12345678")]
    old_session = FakeSession(ip="192.168.1.50", name="旧机")
    sessions = [old_session]
    host = WebHost(cfg, lambda: sessions)

    source = AppConfig()
    source.printers = [
        PrinterInfo(ip="10.0.0.9", name="导进来的", access_code="87654321"),
        PrinterInfo(ip="10.0.0.10", name="第二台", access_code="11112222"),
    ]
    payload = source.to_json("pw")

    result = host.import_config_text(payload, "pw")
    assert result["ok"] is True, result
    assert result["printers"] == 2
    assert old_session.stopped is True, "旧会话必须停掉"
    assert [info.ip for info in cfg.printers] == ["10.0.0.9", "10.0.0.10"]
    assert [session.info.ip for session in sessions] == ["10.0.0.9", "10.0.0.10"]
    assert AppConfig.load().printers[0].ip == "10.0.0.9", "必须落盘"


def test_网页端导入口令错时报错且不动现有配置(isolated_config_dir, monkeypatch):
    factory = _FakeSessionFactory(monkeypatch)
    cfg = AppConfig()
    cfg.printers = [PrinterInfo(ip="192.168.1.50", name="旧机", access_code="12345678")]
    sessions = [FakeSession()]
    host = WebHost(cfg, lambda: sessions)

    source = AppConfig()
    source.printers = [PrinterInfo(ip="10.0.0.9", name="导进来的", access_code="87654321")]
    payload = source.to_json("pw")

    result = host.import_config_text(payload, "错的")
    assert result["ok"] is False
    assert "口令" in result["detail"]
    assert [info.ip for info in cfg.printers] == ["192.168.1.50"], "现有配置不能被破坏"
    assert factory.created == [], "不该建任何会话"


def test_网页端导入空内容给出可读提示(isolated_config_dir):
    host = WebHost(AppConfig(), lambda: [])
    result = host.import_config_text("   ")
    assert result["ok"] is False and result["detail"]


# --------------------------------------------------------------------------- 配置备份的 HTTP 接口


def test_配置备份接口的令牌保护(web_server):
    """契约：导出配置能拿到全部访问代码，必须同样受令牌保护。

    这条还顺带守住一个**只在 Windows 上暴露**的坑：服务端在没读完 POST 请求体
    就回 401 时，keep-alive 连接里留下未读字节，客户端下一次写会撞上
    WinError 10053「连接被主机中的软件中止」（Windows CI 就是这么炸的）。
    见 `_Handler.do_POST` 的说明。
    """
    base, _calls, _sessions = web_server
    for path in ("/api/config/export", "/api/config/import"):
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{base}{path}",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=10,
            )
        assert info.value.code == 401


def test_不支持时接口返回501而不是500(web_server):
    """契约：宿主没注入回调时要明确说"不支持"（前端据此给出可读提示）。"""
    base, _calls, _sessions = web_server
    status, data = _post(base, "/api/config/export", {})
    assert status == 501
    assert data["supported"] is False
    status, data = _post(base, "/api/config/import", {"json": "{}"})
    assert status == 501


# --------------------------------------------------------------------------- 通道诊断（网页/安卓）


def test_网页端能跑通道诊断并返回报告(isolated_config_dir, monkeypatch):
    """契约：桌面端有「通道诊断」对话框，网页/安卓端也要能诊断。

    平板上没法跑 `tools/diagnose.py`；而"画面出不来"正是最需要诊断的场景。
    这里把共用流程（`app/bambu/diagnostics.py`）打成桩，只验证编排与返回结构。
    """
    from app.bambu import diagnostics

    monkeypatch.setattr(
        diagnostics,
        "run",
        lambda *a, **k: iter(
            [
                diagnostics.SectionStart("① 端口连通性"),
                diagnostics.SectionLines(["   8883 MQTT 遥测: 可连接 ✓"]),
                diagnostics.SectionStart("③ 6000 端口画面"),
                diagnostics.SectionLines(["   成功 ✓ 取得 32 KB 画面（0.1s）"]),
            ]
        ),
    )
    session = FakeSession()
    session.info.access_code = "12345678"
    host = WebHost(AppConfig(), lambda: [session])

    result = host.diagnose(0)
    assert result["ok"] is True
    assert result["sections"][0]["title"] == "① 端口连通性"
    assert "可连接" in result["sections"][0]["lines"][0]
    assert result["sections"][1]["lines"] == ["   成功 ✓ 取得 32 KB 画面（0.1s）"]
    assert result["tail"], "要给收尾提示（401 说明 RTSP 服务是活的）"


def test_网页端诊断缺访问代码时给出可执行提示(isolated_config_dir):
    session = FakeSession()
    session.info.access_code = ""
    host = WebHost(AppConfig(), lambda: [session])
    result = host.diagnose(0)
    assert result["ok"] is False
    assert "访问代码" in result["detail"]


def test_网页端诊断索引越界(isolated_config_dir):
    host = WebHost(AppConfig(), lambda: [])
    result = host.diagnose(3)
    assert result["ok"] is False and "共 0 台" in result["detail"]


# --------------------------------------------------------------------------- 画面大小


def test_网页端可以设置重点画面(isolated_config_dir):
    """契约：网页/安卓端能把这台设备设成重点画面（2×2），桌面端原有能力）。"""
    session = FakeSession(ip="192.168.1.50")
    cfg = AppConfig()
    cfg.printers = [PrinterInfo(ip="192.168.1.50", name="测试机", access_code="12345678")]
    host = WebHost(cfg, lambda: [session])

    result = host.set_tile_span(0, 2)
    assert result["ok"] is True
    assert session.info.tile_span == 2
    assert AppConfig.load().printers[0].tile_span == 2, "必须落盘（重启后要保住）"

    assert host.set_tile_span(0, 1)["ok"] is True
    assert AppConfig.load().printers[0].tile_span == 1

    # 越界取值会被夹住，而不是写进非法值
    host.set_tile_span(0, 9)
    assert session.info.tile_span == 3


def test_网页端诊断与布局接口在不支持时返回501(web_server):
    base, _calls, _sessions = web_server
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(f"{base}/api/diagnose?index=0&token=secret", timeout=10)
    assert info.value.code == 501
    status, data = _post(base, "/api/layout", {"index": 0, "span": 2})
    assert status == 501
    assert data["supported"] is False
