"""Moonraker / Klipper 适配器契约（离线，用假服务器端到端验证）。

覆盖目标：Snapmaker U1 与所有 Moonraker 机器。这里锁定的都是**协议事实**，
每条都对应 `docs/FIELD_NOTES.md` 里的一条调研结论或真机实测：

1. 响应是 ``{"result": {"status": {...}}}`` **两层嵌套**（写错会永远解析出空）；
2. ``virtual_sdcard.progress`` 是 **0.0–1.0**（拓竹是 0–100，必须换算）；
3. 层数缺失时保持**未知**而不是报 0（切片器没写 SET_PRINT_STATS_INFO 就没有）；
4. 控制分两路：``pause/resume/cancel`` 走 HTTP；**急停与 ``control/*`` 只能走 WS**；
5. WS 调灯的参数名是 ``name``（不是 ``led``），且只能 0/1；
6. **U1 的摄像头不周期性保活就一直是陈旧帧** —— 这条用假服务器模拟真机行为。

测试全部离线：假服务器绑在 127.0.0.1 的随机端口，不触碰任何真实设备。
"""

from __future__ import annotations

import time

import pytest

from app.adapters.moonraker.adapter import (
    QUERY_OBJECTS,
    STATE_MAP,
    MoonrakerAdapter,
    parse_status,
)
from app.adapters.moonraker.fake import TINY_JPEG, FakeMoonraker
from app.adapters.moonraker.ws import WebSocketClient, WebSocketError
from app.bambu.models import PrinterInfo
from app.core import DeviceSession
from app.core.device import (
    JOB_FAILED,
    JOB_FINISHED,
    JOB_IDLE,
    JOB_OFFLINE,
    JOB_PAUSED,
    JOB_PRINTING,
    JOB_UNKNOWN,
)

ACCESS_CODE = "12345678"


def make_info(name: str = "U1 假机") -> PrinterInfo:
    return PrinterInfo(ip="127.0.0.1", name=name)


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --------------------------------------------------------------------------- 纯解析


@pytest.mark.parametrize(
    "raw_state, expected",
    [
        ("standby", JOB_IDLE),
        ("printing", JOB_PRINTING),
        ("paused", JOB_PAUSED),
        ("complete", JOB_FINISHED),
        ("cancelled", JOB_IDLE),
        ("error", JOB_FAILED),
        ("whatever", JOB_UNKNOWN),
    ],
)
def test_状态机映射(raw_state, expected):
    """契约：Moonraker 的状态机取值映射到归一化作业状态。

    各家状态机取值完全不同（Moonraker 是字符串、Creality 是 0..5、
    Elegoo 是 0..22），所以映射必须由适配器负责，通用层只认 JOB_* 常量。
    """
    fields = parse_status({"status": {"print_stats": {"state": raw_state}}})
    assert fields["job_state"] is expected


def test_响应是两层嵌套且被正确解开():
    """契约：真实响应形如 ``{"result": {"status": {...}}}``。

    只找顶层 ``status`` 会永远解析出空字典 —— 这是实现时最容易犯的错。
    """
    wrapped = {
        "result": {
            "status": {
                "print_stats": {"state": "printing", "filename": "a.gcode"},
                "virtual_sdcard": {"progress": 0.5},
                "extruder": {"temperature": 200.0, "target": 210.0},
                "heater_bed": {"temperature": 55.0, "target": 60.0},
            }
        }
    }
    fields = parse_status(wrapped)
    assert fields["progress_percent"] == 50
    assert fields["nozzle_temper"] == 200.0
    assert fields["bed_temper"] == 55.0
    assert fields["job_name"] == "a.gcode"


def test_也接受直接传内层status():
    """契约：直接传 ``status`` 或内层对象也能解析（便于复用与单元测试）。"""
    inner = {"print_stats": {"state": "printing"}, "virtual_sdcard": {"progress": 0.25}}
    assert parse_status(inner)["progress_percent"] == 25
    assert parse_status({"status": inner})["progress_percent"] == 25


def test_进度是小数必须换算成百分比():
    """契约：``virtual_sdcard.progress`` 是 0.0–1.0，要换算成 0–100。

    拓竹的 `mc_percent` 是 0–100 整数；若这里忘了乘 100，用户会看到 0% 或 1%。
    """
    for fraction, percent in ((0.0, 0), (0.42, 42), (1.0, 100), (1.5, 100), (-0.2, 0)):
        fields = parse_status({"status": {"virtual_sdcard": {"progress": fraction}}})
        assert fields["progress_percent"] == percent, f"{fraction} 应换算为 {percent}"


def test_进度回退到display_status():
    """契约：没有 virtual_sdcard 时用 ``display_status.progress``（M73 指令的进度）。"""
    fields = parse_status({"status": {"display_status": {"progress": 0.75}}})
    assert fields["progress_percent"] == 75


def test_层数缺失时保持未知而不是零():
    """契约：切片器没写 ``SET_PRINT_STATS_INFO`` 时 ``print_stats.info`` 为空，
    此时**不要**写 layer_current/layer_total。

    写 0 会让界面显示"层 0/0"，与"未知"混淆。
    """
    fields = parse_status({"status": {"print_stats": {"state": "printing", "info": {}}}})
    assert "layer_current" not in fields
    assert "layer_total" not in fields
    assert fields["job_state"] is JOB_PRINTING


def test_层数存在时被解析():
    """契约：info 里有层数时正常解析。"""
    fields = parse_status(
        {"status": {"print_stats": {"info": {"current_layer": 12, "total_layer": 240}}}}
    )
    assert fields["layer_current"] == 12
    assert fields["layer_total"] == 240


def test_klippy异常时标为离线():
    """契约：``webhooks.state`` 处于 shutdown/error/startup 时，设备标为离线。

    此时设备连状态查询都未必回应，显示一个可疑读数不如诚实地说"离线"。
    """
    for bad in ("shutdown", "error", "startup"):
        fields = parse_status(
            {"status": {"webhooks": {"state": bad}, "print_stats": {"state": "printing"}}}
        )
        assert fields["job_state"] is JOB_OFFLINE, f"{bad} 应判为离线"


def test_klippy就绪时按状态机判定():
    """契约：``webhooks.state == ready`` 时按 print_stats 判定。"""
    fields = parse_status(
        {"status": {"webhooks": {"state": "ready"}, "print_stats": {"state": "paused"}}}
    )
    assert fields["job_state"] is JOB_PAUSED


@pytest.mark.parametrize(
    "objects",
    [
        {},
        {"result": {}},
        {"result": {"status": {}}},
        {"result": {"status": {"print_stats": "not-a-dict"}}},
        {"result": {"status": {"virtual_sdcard": None}}},
        {"result": "garbage"},
        [],
        None,
        123,
    ],
)
def test_畸形输入不抛异常(objects):
    """契约：任何畸形响应都只能返回字典（可能是空的），绝不能抛异常打断轮询。"""
    result = parse_status(objects)
    assert isinstance(result, dict)


def test_异常类型字段被忽略而不是崩掉():
    """契约：字段类型不对（字符串温度、None 进度）时跳过该字段，其它字段照常解析。"""
    fields = parse_status(
        {
            "status": {
                "extruder": {"temperature": "很热", "target": None},
                "heater_bed": {"temperature": 60.0},
                "virtual_sdcard": {"progress": "half"},
                "print_stats": {"state": "printing"},
            }
        }
    )
    assert "nozzle_temper" not in fields
    assert fields["bed_temper"] == 60.0
    assert "progress_percent" not in fields
    assert fields["job_state"] is JOB_PRINTING


def test_查询对象清单覆盖关键字段():
    """契约：一次查询要带上状态机、进度、两个温度、webhooks。"""
    assert set(QUERY_OBJECTS) >= {
        "print_stats",
        "virtual_sdcard",
        "extruder",
        "heater_bed",
        "webhooks",
    }


def test_状态映射表覆盖官方全部取值():
    """契约：官方文档列出的 6 个状态都要有映射。"""
    assert set(STATE_MAP) == {
        "standby",
        "printing",
        "paused",
        "complete",
        "cancelled",
        "error",
    }


# --------------------------------------------------------------------------- 端到端


def test_适配器满足设备会话协议():
    """契约：第三方适配器满足 `DeviceSession`，界面与网页无需改动即可用。"""
    with FakeMoonraker() as fake:
        adapter = MoonrakerAdapter(make_info(), base_url=fake.base_url)
        try:
            assert isinstance(adapter, DeviceSession)
        finally:
            adapter.stop()


def test_端到端遥测全链路():
    """契约：从假 Moonraker 拉到状态并正确落到通用状态对象。"""
    with FakeMoonraker() as fake:
        adapter = MoonrakerAdapter(make_info(), base_url=fake.base_url, poll_interval=0.1)
        try:
            adapter.start()
            assert _wait_until(lambda: adapter.snapshot().online)
            status = adapter.snapshot()
            assert status.job_state is JOB_PRINTING
            assert status.progress_percent == 42
            assert status.job_name == "benchy.gcode"
            assert status.layer_current == 12
            assert status.layer_total == 240
            assert status.nozzle_temper == 245.3
            assert status.bed_temper == 60.0
            assert status.job_state_text == "打印中"
        finally:
            adapter.stop()


def test_控制走HTTP且被设备收到():
    """契约：pause / resume / stop 走 HTTP POST，且真的到达设备。"""
    with FakeMoonraker() as fake:
        adapter = MoonrakerAdapter(make_info(), base_url=fake.base_url, poll_interval=0.1)
        try:
            adapter.start()
            assert _wait_until(lambda: adapter.can_control)
            assert adapter.pause_print() is True
            assert _wait_until(lambda: "pause" in fake.commands)
            assert adapter.resume_print() is True
            assert _wait_until(lambda: "resume" in fake.commands)
            assert adapter.stop_print() is True
            assert _wait_until(lambda: "cancel" in fake.commands)
        finally:
            adapter.stop()


def test_没有灯名时不声明灯控能力():
    """契约：不知道灯的 Moonraker 对象名时**不声明**灯控能力。

    猜一个名字会发出必然失败的请求（真实 Moonraker 会因缺必填项报错），
    不如让界面不显示灯按钮。
    """
    with FakeMoonraker() as fake:
        adapter = MoonrakerAdapter(make_info(), base_url=fake.base_url)
        try:
            assert adapter.capabilities.can_control_light is False
            assert adapter.set_light(True) is False
            assert fake.ws_calls == [], "不应发出任何灯控请求"
        finally:
            adapter.stop()


def test_灯控走WebSocket且参数名是name():
    """契约：灯控只能走 WebSocket，且参数名是 ``name``（不是 ``led``）。

    官方源码：WS 层的 ``printer.control.led`` 用 ``web_request.get_str('name')``，
    传 ``led`` 会因缺必填项报错。另外该层用整数取色 → 只能 0/1 开关。
    """
    with FakeMoonraker() as fake:
        adapter = MoonrakerAdapter(
            make_info(),
            base_url=fake.base_url,
            ws_url=fake._ws.url,
            led_name="cavity_led",
            poll_interval=0.1,
        )
        try:
            adapter.start()
            assert _wait_until(lambda: adapter.can_control)
            assert adapter.set_light(True) is True
            assert _wait_until(lambda: fake.led is True)
            assert fake.ws_calls[-1] == (
                "printer.control.led",
                {"name": "cavity_led", "white": 1},
            )
            assert adapter.set_light(False) is True
            assert _wait_until(lambda: fake.led is False)
        finally:
            adapter.stop()


def test_速度档位映射成百分比走WebSocket():
    """契约：速度档位（1-4）映射成百分比并经 WS 下发，参数名是 ``percentage``。"""
    with FakeMoonraker() as fake:
        adapter = MoonrakerAdapter(
            make_info(), base_url=fake.base_url, ws_url=fake._ws.url, poll_interval=0.1
        )
        try:
            adapter.start()
            assert _wait_until(lambda: adapter.can_control)
            assert adapter.set_speed(3) is True
            assert _wait_until(lambda: fake.speed_factor is not None)
            method, params = fake.ws_calls[-1]
            assert method == "printer.control.print_speed"
            assert "percentage" in params and "speed" not in params
        finally:
            adapter.stop()


def test_急停走WebSocket():
    """契约：急停**只能**走 WebSocket（官方该端点带 ``transports = all & ~HTTP``）。

    HTTP-only 的客户端无法急停 —— 所以这条通道必须常驻，不能省。
    """
    with FakeMoonraker() as fake:
        adapter = MoonrakerAdapter(
            make_info(), base_url=fake.base_url, ws_url=fake._ws.url, poll_interval=0.1
        )
        try:
            adapter.start()
            assert _wait_until(lambda: adapter.can_control)
            assert adapter.emergency_stop() is True
            assert _wait_until(lambda: fake.emergency_stopped)
        finally:
            adapter.stop()


# --------------------------------------------------------------------------- U1 摄像头


def test_U1摄像头不保活就取不到画面():
    """契约（真机实测行为）：U1 的快照在不周期性保活时是陈旧的，拿不到新帧。

    这条用假服务器复现真机行为：``camera_requires_monitor=True`` 时
    ``monitor.jpg`` 在收到 ``camera.start_monitor`` 之前返回 404。
    """
    with FakeMoonraker(camera_requires_monitor=True) as fake:
        adapter = MoonrakerAdapter(
            make_info(),
            base_url=fake.base_url,
            ws_url=fake._ws.url,
            poll_interval=0.1,
            max_fps=20.0,
        )
        try:
            adapter.start()
            # 保活生效后画面应当出现
            assert _wait_until(lambda: adapter.snapshot().camera_online, timeout=6.0), (
                "保活后仍取不到画面"
            )
            assert fake.keepalive_seen >= 1, "应当发送过 camera.start_monitor 保活"
            seq, frame = adapter.latest_frame()
            assert seq >= 1
            assert frame is not None and frame.startswith(b"\xff\xd8")
        finally:
            adapter.stop()


def test_保活不依赖用户配置也能工作():
    """契约：默认就该保活（不需要用户额外配置）——否则 U1 用户永远看不到画面。"""
    with FakeMoonraker(camera_requires_monitor=True) as fake:
        adapter = MoonrakerAdapter(
            make_info(), base_url=fake.base_url, ws_url=fake._ws.url, poll_interval=0.1
        )
        try:
            adapter.start()
            assert _wait_until(lambda: fake.keepalive_seen >= 1, timeout=6.0)
            calls = [method for method, _ in fake.ws_calls]
            assert "camera.start_monitor" in calls
        finally:
            adapter.stop()


def test_摄像头自动发现走webcams_list():
    """契约：未显式给摄像头 URL 时，用官方 ``/server/webcams/list`` 自动发现。"""
    with FakeMoonraker(camera="list") as fake:
        adapter = MoonrakerAdapter(
            make_info(), base_url=fake.base_url, ws_url=fake._ws.url, poll_interval=0.1,
            max_fps=20.0,
        )
        try:
            adapter.start()
            assert _wait_until(
                lambda: any(p == "/server/webcams/list" for _, p in fake.requests)
            ), "应当探测过 webcams/list"
            assert _wait_until(lambda: adapter.capabilities.has_camera, timeout=5.0)
            assert adapter.video_backend == "快照"
        finally:
            adapter.stop()


def test_没有摄像头时退化为只监控():
    """契约：设备没有摄像头时不得刷错误，只是没有画面。

    这是第三方设备的常见形态（PrusaLink 只有静态快照、不少 Klipper 机器没装摄像头）。
    """
    with FakeMoonraker(camera="none") as fake:
        adapter = MoonrakerAdapter(
            make_info(), base_url=fake.base_url, ws_url=fake._ws.url, poll_interval=0.1
        )
        try:
            adapter.start()
            assert _wait_until(lambda: adapter.snapshot().online)
            status = adapter.snapshot()
            assert status.camera_online is False
            assert status.online is True, "没有摄像头不应影响遥测在线"
            assert fake.keepalive_seen == 0, "没有摄像头就不该发保活"
        finally:
            adapter.stop()


def test_摄像头返回非JPEG时被拒绝():
    """契约：只接受 JPEG 帧。

    上层（`ui/frame_decoder.py` 与网页转码）都按 JPEG 解码，
    放进非 JPEG 数据只会让画面静默变黑。
    """
    with FakeMoonraker() as fake:
        adapter = MoonrakerAdapter(
            make_info(),
            base_url=fake.base_url,
            camera_url=f"{fake.base_url}/not-jpeg",
            poll_interval=0.1,
            max_fps=20.0,
        )
        try:
            adapter.start()
            time.sleep(0.4)  # 给画面轮询几次机会（假端点一直返回非 JPEG）
            assert adapter.snapshot().camera_online is False
            assert adapter.latest_frame()[1] is None
        finally:
            adapter.stop()


def test_假服务器的JPEG常量是合法JPEG头():
    """契约：测试用的假帧必须是真 JPEG（FFD8FF…FFD9），否则"取到画面"的断言没有意义。"""
    assert TINY_JPEG.startswith(b"\xff\xd8\xff")
    assert TINY_JPEG.endswith(b"\xff\xd9")


# --------------------------------------------------------------------------- 鉴权与容错


def test_需要APIKey时缺少凭据会被记成失败():
    """契约：设备要求 API Key 而没配时，应当失败并记告警，而不是静默无数据。

    非标准网段（例如 77.x）会让 Moonraker 判定不受信并返回 401 —— 真实场景。
    """
    with FakeMoonraker(api_key="secret-key") as fake:
        adapter = MoonrakerAdapter(make_info(), base_url=fake.base_url, poll_interval=0.1)
        try:
            adapter.start()
            assert _wait_until(lambda: adapter.warnings, timeout=5.0), "应记录一条告警"
            assert adapter.snapshot().online is False
        finally:
            adapter.stop()


def test_带正确APIKey可以连上():
    """契约：配置了正确的 API Key 就能正常取到状态（走 X-Api-Key 头）。"""
    with FakeMoonraker(api_key="secret-key") as fake:
        adapter = MoonrakerAdapter(
            make_info(), base_url=fake.base_url, api_key="secret-key", poll_interval=0.1
        )
        try:
            adapter.start()
            assert _wait_until(lambda: adapter.snapshot().online)
            assert adapter.snapshot().progress_percent == 42
        finally:
            adapter.stop()


def test_设备不可达时退避且不崩():
    """契约：设备离线时轮询继续但退避，warnings 只记一条（由基类保证）。"""
    adapter = MoonrakerAdapter(make_info(), base_url="http://127.0.0.1:1", poll_interval=0.05)
    try:
        adapter.start()
        assert _wait_until(lambda: len(adapter.warnings) == 1, timeout=5.0)
        time.sleep(0.2)
        assert len(adapter.warnings) == 1, "告警不应被刷满"
        assert adapter.running is True, "单次失败不应停止会话"
    finally:
        adapter.stop()


def test_stop会关闭WebSocket连接():
    """契约：stop() 必须把 WebSocket 一起关掉，否则会残留连接（真机上有连接数限制）。"""
    with FakeMoonraker() as fake:
        adapter = MoonrakerAdapter(
            make_info(), base_url=fake.base_url, ws_url=fake._ws.url, poll_interval=0.1
        )
        adapter.start()
        assert _wait_until(lambda: adapter.can_control)
        # 造出一条真实的 WS 连接（速度控制只能走 WS）
        assert adapter.set_speed(2) is True
        assert adapter._ws is not None, "应当已建立 WS 连接"
        adapter.stop()
        assert adapter._ws is None, "stop 后应当关掉 WS 连接"


# --------------------------------------------------------------------------- WebSocket 客户端


def test_websocket客户端能完成握手与调用():
    """契约：自研的极简 WS 客户端能完成 RFC 6455 握手并发 JSON-RPC 调用。

    服务端会校验 Sec-WebSocket-Accept 与客户端掩码，因此帧编解码写错会立刻失败。
    """
    with FakeMoonraker() as fake:
        client = WebSocketClient(fake._ws.url, timeout=3.0)
        try:
            client.connect()
            assert client.connected
            result = client.call("printer.control.print_speed", {"percentage": 150})
            assert result == "ok"
            assert fake.speed_factor == 150
        finally:
            client.close()
        assert not client.connected


def test_websocket未知方法返回错误():
    """契约：服务端返回 error 时客户端要抛 WebSocketError（而不是返回 None 掩盖）。"""
    with FakeMoonraker() as fake:
        client = WebSocketClient(fake._ws.url, timeout=3.0)
        try:
            client.connect()
            with pytest.raises(WebSocketError):
                client.call("totally.unknown.method")
        finally:
            client.close()


def test_websocket连到非WS端口会失败():
    """契约：连到不是 WebSocket 的端口时明确失败，不能挂死。"""
    with FakeMoonraker() as fake:
        client = WebSocketClient(f"ws://127.0.0.1:{fake.port}/websocket", timeout=2.0)
        with pytest.raises((WebSocketError, OSError)):
            client.connect()
        client.close()
