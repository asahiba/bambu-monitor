"""``PrinterSession`` 的接口层测试（``app/bambu/printer.py``）。

覆盖的契约：

* ``_preferred_channel()``：按机型能力（P1S 只有 6000、X2D 只有 RTSPS）与
  ``stream_mode`` 显式覆盖（tcp6000 / rtsp）选择视频通道
* ``can_control``：未启动遥测（``_mqtt is None``）时必须为 False
* 控制指令（pause/resume/stop/set_light/set_speed）在 ``_mqtt is None`` 时
  返回 False 且**不抛异常**；遥测在线时透传给 ``MqttWorker.publish_command``
* ``snapshot()`` 返回 ``PrinterStatus``；``connection_summary()`` 离线时为「离线」
* 没有视频流时：``video_backend == "-"``、``camera_fps == 0.0``、``latest_frame() == (0, None)``
* 状态回调：``_handle_report`` / ``_handle_mqtt_state`` / ``_handle_stream_state``

⚠️ 本文件**绝不**调用 ``start()`` 去真正连打印机：唯一涉及 ``start()`` 的用例
把 IP / 序列号 / 访问代码全部留空，此时实现只会记告警、不会建立任何连接。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest

from app.bambu.models import PrinterInfo, PrinterModel, PrinterStatus
from app.bambu.printer import PrinterSession
from app.bambu.rtsp import RtspStream


def make_info(**overrides) -> PrinterInfo:
    """构造一份不会指向任何真实设备的信息（IP 留空）。"""
    data = {
        "ip": "",
        "serial": "",
        "name": "单测机",
        "model": PrinterModel.P1S,
        "access_code": "",
        "stream_mode": "auto",
    }
    data.update(overrides)
    return PrinterInfo(**data)


def make_session(**overrides) -> PrinterSession:
    return PrinterSession(make_info(**overrides))


class FakeMqtt:
    """``MqttWorker`` 的最小替身：只记录下发的控制命令。"""

    def __init__(self, result: bool = True) -> None:
        self.commands: list[tuple] = []
        self.stopped = False
        self._result = result

    def publish_command(self, section: str, command: str, **fields) -> bool:
        self.commands.append((section, command, fields))
        return self._result

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture()
def rtsp_available(monkeypatch):
    """强制让 ``RtspStream.available()`` 返回指定值，避免依赖 opencv 是否安装。"""

    def _set(value: bool) -> None:
        monkeypatch.setattr(RtspStream, "available", staticmethod(lambda: value))

    return _set


# ------------------------------------------------------------------ 视频通道
@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (PrinterModel.P1S, "tcp6000"),
        (PrinterModel.P1P, "tcp6000"),
        (PrinterModel.A1, "tcp6000"),
        (PrinterModel.A1MINI, "tcp6000"),
        (PrinterModel.X2D, "rtsp"),
        (PrinterModel.H2D, "rtsp"),
        (PrinterModel.P2S, "rtsp"),
    ],
)
def test_preferred_channel_按机型能力选择(model, expected, rtsp_available):
    """契约：P1/A1 系列只有 6000 端口画面；X2D/H2D/P2S 只提供 RTSPS(322)。"""
    rtsp_available(True)
    assert make_session(model=model)._preferred_channel() == expected


@pytest.mark.parametrize(
    ("model", "stream_mode", "expected"),
    [
        (PrinterModel.X2D, "tcp6000", "tcp6000"),
        (PrinterModel.P1S, "rtsp", "rtsp"),
        (PrinterModel.P1S, "TCP6000", "tcp6000"),
        (PrinterModel.X2D, "RTSP", "rtsp"),
    ],
)
def test_preferred_channel_显式stream_mode覆盖机型默认(model, stream_mode, expected, rtsp_available):
    """契约：``stream_mode`` 显式指定时优先于机型默认值（大小写不敏感）。"""
    rtsp_available(True)
    session = make_session(model=model, stream_mode=stream_mode)
    assert session._preferred_channel() == expected


def test_preferred_channel_无opencv时RTSPS机型降级并告警(rtsp_available):
    """契约：X2D 这类只有 RTSPS 的机型在缺少 opencv 时降级到 tcp6000，并写入告警。"""
    rtsp_available(False)
    session = make_session(model=PrinterModel.X2D)
    assert session._preferred_channel() == "tcp6000"
    assert any("opencv" in text for text in session.warnings)


def test_preferred_channel_自动模式的X1C优先RTSPS(rtsp_available):
    """契约：X1C 的 video_channel 是 auto：有 opencv 时优先 RTSPS，没有则退回 6000。"""
    rtsp_available(True)
    assert make_session(model=PrinterModel.X1C)._preferred_channel() == "rtsp"
    rtsp_available(False)
    assert make_session(model=PrinterModel.X1C)._preferred_channel() == "tcp6000"


# ------------------------------------------------------------------ 未启动状态
def test_未启动时can_control为假():
    """契约：还没有建立遥测（_mqtt is None）时不允许下发控制命令。"""
    session = make_session()
    assert session._mqtt is None
    assert session.can_control is False
    assert session.running is False


def test_未启动时控制指令返回假且不抛异常():
    """契约：遥测未连接时 pause/resume/stop/set_light/set_speed 一律返回 False，
    且绝不能抛异常（界面会在任意时刻点这些按钮）。"""
    session = make_session()
    assert session.pause_print() is False
    assert session.resume_print() is False
    assert session.stop_print() is False
    assert session.set_light(True) is False
    assert session.set_light(False) is False
    assert session.set_speed(1) is False
    assert session.set_speed(4) is False


@pytest.mark.parametrize("level", [0, -3, 9, 100])
def test_set_speed档位越界不抛异常(level):
    """契约：set_speed() 收到越界档位也必须安全返回 False，不能抛异常。"""
    session = make_session()
    assert session.set_speed(level) is False


def test_没有视频流时的只读属性():
    """契约：没有任何视频流时 video_backend 为 "-"、camera_fps 为 0.0、
    latest_frame() 返回 (0, None)。"""
    session = make_session()
    assert session.video_backend == "-"
    assert session.camera_fps == 0.0
    seq, frame = session.latest_frame()
    assert seq == 0 and frame is None


def test_没有视频流时设置画面参数不抛异常():
    """契约：set_video_target_size()/set_max_fps() 在没有流时是安全的空操作。"""
    session = make_session()
    session.set_video_target_size(640, 360)
    session.set_max_fps(10.0)
    session.set_max_fps(0)
    assert session.latest_frame() == (0, None)


def test_snapshot返回PrinterStatus且离线摘要():
    """契约：snapshot() 返回 PrinterStatus；遥测与画面都离线时摘要为「离线」。"""
    session = make_session()
    status = session.snapshot()
    assert isinstance(status, PrinterStatus)
    assert status.mqtt_online is False
    assert status.camera_online is False
    assert session.connection_summary() == "离线"


def test_connection_summary各组合():
    """契约：只有一路在线时为「部分在线」，两路都在线时为「在线」。"""
    session = make_session()
    session.status.mqtt_online = True
    assert session.connection_summary() == "部分在线"
    session.status.camera_online = True
    assert session.connection_summary() == "在线"
    session.status.mqtt_online = False
    assert session.connection_summary() == "部分在线"


def test_stop未启动的会话不抛异常():
    """契约（回归）：从未 start() 过的会话调用 stop() 必须安全返回，
    不得因为 join 未启动的视频线程而抛异常。"""
    session = make_session()
    session.stop()
    assert session.running is False
    assert session.status.mqtt_online is False
    assert session.status.camera_online is False
    session.stop()  # 重复 stop 也应安全


# --------------------------------------------------------------- 遥测在线之后
def test_遥测在线时控制指令透传():
    """契约：_mqtt 存在且 status.mqtt_online 为真时，控制指令透传给
    MqttWorker.publish_command，并把返回值原样返回。"""
    session = make_session()
    worker = FakeMqtt()
    session._mqtt = worker
    session.status.mqtt_online = True
    assert session.can_control is True

    assert session.pause_print() is True
    assert session.resume_print() is True
    assert session.stop_print() is True
    assert worker.commands[0] == ("print", "pause", {"param": ""})
    assert worker.commands[1] == ("print", "resume", {})
    assert worker.commands[2] == ("print", "stop", {})


def test_set_speed档位钳制后下发():
    """契约：set_speed 把档位夹到 1..4 后以字符串 param 下发。"""
    session = make_session()
    worker = FakeMqtt()
    session._mqtt = worker
    session.status.mqtt_online = True

    assert session.set_speed(9) is True
    assert session.set_speed(0) is True
    assert session.set_speed(3) is True
    assert [item[2]["param"] for item in worker.commands] == ["4", "1", "3"]
    assert all(item[:2] == ("print", "print_speed") for item in worker.commands)


def test_set_light下发灯光指令():
    """契约：set_light 通过 system/ledctrl 控制 chamber_light 的 on/off。"""
    session = make_session()
    worker = FakeMqtt()
    session._mqtt = worker
    session.status.mqtt_online = True

    assert session.set_light(True) is True
    assert session.set_light(False) is True
    on_fields = worker.commands[0][2]
    off_fields = worker.commands[1][2]
    assert worker.commands[0][:2] == ("system", "ledctrl")
    assert on_fields["led_node"] == "chamber_light"
    assert on_fields["led_mode"] == "on"
    assert off_fields["led_mode"] == "off"


def test_遥测离线时即使有worker也不下发():
    """契约：_mqtt 存在但 mqtt_online 为假时仍然拒绝下发（返回 False）。"""
    session = make_session()
    worker = FakeMqtt()
    session._mqtt = worker
    session.status.mqtt_online = False
    assert session.can_control is False
    assert session.pause_print() is False
    assert worker.commands == []


def test_worker下发失败时透传False():
    """契约：MqttWorker.publish_command 返回 False 时，控制方法也返回 False。"""
    session = make_session()
    session._mqtt = FakeMqtt(result=False)
    session.status.mqtt_online = True
    assert session.pause_print() is False


# ------------------------------------------------------------------ 状态回调
def test_handle_report更新状态与固件():
    """契约：_handle_report 合并 print 报文（进度/温度/HMS），并把 info.module 里
    ota 的 sw_ver 采纳为固件版本，同时把 mqtt_online 置为 True。"""
    session = make_session(serial="01P00A1234567")
    assert session.info.firmware == ""

    session._handle_report(
        {
            "print": {
                "gcode_state": "RUNNING",
                "mc_percent": 37,
                "mc_remaining_time": 42,
                "nozzle_temper": 219.4,
                "nozzle_target_temper": 220,
                "bed_temper": 59.1,
                "bed_target_temper": 60,
                "subtask_name": "件.gcode.3mf",
                "hms": [{"attr": 0x03000100, "code": 0x00010001}],
            },
            "info": {"module": [{"name": "ota", "sw_ver": "01.05.00.00"}]},
        }
    )

    status = session.snapshot()
    assert status.mqtt_online is True
    assert status.progress == 37
    assert status.remaining_minutes == 42
    assert status.nozzle_temper == pytest.approx(219.4)
    assert status.gcode_state == "RUNNING"
    assert status.is_printing is True
    assert status.state_text == "打印中"
    assert len(status.hms_items) == 1
    assert session.info.firmware == "01.05.00.00"
    assert session.connection_summary() == "部分在线"


def test_handle_report只用最新固件覆盖():
    """契约：后续报文里 ota 版本变化时固件字段跟随更新。"""
    session = make_session(serial="01P00A1234567")
    session._handle_report({"info": {"module": [{"name": "ota", "sw_ver": "01.05.00.00"}]}})
    assert session.info.firmware == "01.05.00.00"
    session._handle_report({"info": {"module": [{"name": "ota", "sw_ver": "01.06.00.00"}]}})
    assert session.info.firmware == "01.06.00.00"


def test_handle_mqtt_state切换在线与认证错误():
    """契约：_handle_mqtt_state 负责把 MqttWorker 的状态映射到
    status.mqtt_online / mqtt_auth_error / last_error。"""
    session = make_session()
    session._handle_mqtt_state("online", "遥测已连接")
    assert session.status.mqtt_online is True
    assert session.mqtt_auth_error is False

    session._handle_mqtt_state("auth_error", "访问代码错误或未被授权（MQTT 拒绝连接）")
    assert session.status.mqtt_online is False
    assert session.mqtt_auth_error is True
    assert "访问代码错误" in session.status.last_error


def test_handle_stream_state更新画面状态():
    """契约：只有 streaming 才算画面在线；其它状态一律置为不在线。"""
    session = make_session(model=PrinterModel.A1)
    session._handle_stream_state(None, "streaming", "视频已连接")
    assert session.status.camera_online is True
    assert session.last_camera_state == "streaming"
    assert session.last_camera_detail == "视频已连接"

    session._handle_stream_state(None, "retrying", "视频流中断，正在重连")
    assert session.status.camera_online is False
    assert session.last_camera_state == "retrying"


def test_handle_stream_state认证错误在支持RTSPS机型上改写文案():
    """契约：遥测已连上（说明访问代码没错）且机型支持 RTSPS 时，
    6000 端口的 auth_error 应改写为「改用 RTSPS 通道」，避免误导用户去改口令。"""
    session = make_session(model=PrinterModel.X1C)
    session.status.mqtt_online = True
    session._handle_stream_state(None, "auth_error", "打印机拒绝连接：请检查访问代码")
    assert "RTSPS" in session.last_camera_detail
    assert session.status.camera_online is False
    assert session.status.last_error == session.last_camera_detail

    # P1S 不支持 RTSPS：不应被改写
    p1s = make_session(model=PrinterModel.P1S)
    p1s.status.mqtt_online = True
    p1s._handle_stream_state(None, "auth_error", "打印机拒绝连接：请检查访问代码")
    assert p1s.last_camera_detail == "打印机拒绝连接：请检查访问代码"


def test_handle_stream_state忽略已废弃通道的回调():
    """契约：来自已被替换掉的通道（id 不在活动集合里）的状态回调必须被丢弃。"""
    session = make_session()
    stale_source = object()
    session._handle_stream_state(stale_source, "streaming", "旧通道")
    assert session.last_camera_state != "streaming"
    assert session.status.camera_online is False


def test_仅有MQTT但缺少访问代码时只记告警不联网():
    """契约：未填写访问代码时 start() 不会启动视频通道，只记录一条告警；
    没有序列号时也不会创建 MQTT 连接（全程零网络访问）。

    注意：本用例故意把 IP/序列号/访问代码全部留空，实现不会发起任何连接。
    """
    session = PrinterSession(PrinterInfo(ip="", serial="", name="空配置"))
    try:
        session.start()
        assert session.running is True
        assert session._camera is None
        assert session._rtsp is None
        assert session._mqtt is None
        assert session.can_control is False
        assert any("访问代码" in text for text in session.warnings)
        assert any("序列号" in text for text in session.warnings)
    finally:
        session.stop()
    assert session.running is False


def test_未配置访问代码时不启动视频通道(monkeypatch):
    """契约：即使有序列号，未填写访问代码时 start() 也不会启动视频通道与看门狗，
    只记录一条告警（本用例把 _start_telemetry 换成空实现，确保零网络访问）。"""
    telemetry_calls: list[str] = []
    monkeypatch.setattr(
        PrinterSession, "_start_telemetry", lambda self: telemetry_calls.append(self.info.serial)
    )

    session = PrinterSession(PrinterInfo(ip="127.0.0.1", serial="01P00A1234567", name="无机密码"))
    try:
        session.start()
        assert telemetry_calls == ["01P00A1234567"], "遥测仍应尝试建立"
        assert session._camera is None and session._rtsp is None
        assert session.video_backend == "-"
        assert session._watchdog is None, "没有访问代码就不该启动视频看门狗"
        assert any("访问代码" in text for text in session.warnings)
    finally:
        session.stop()


# --------------------------------------------------------------- Developer Mode 门槛


def test_控制被固件签名要求挡住时可以被识别():
    """契约：新机型要求 MQTT 命令签名时，`controls_blocked` 为真且给出可读原因。

    实测事实：A2L（固件 01.01.05.00）的 `fun` = "100d122002fbd"，bit 0x20000000 置位，
    表示「MQTT 命令需签名校验」。未在打印机触屏开启 Developer Mode 时，下发的
    暂停/停止/开灯会被固件**静默忽略**——所以必须让界面知道这件事并置灰按钮，
    而不是让用户反复点击一个看起来可用的按钮。
    """
    session = PrinterSession(
        PrinterInfo(ip="127.0.0.1", serial="26A00A000000000000", model=PrinterModel.A2L)
    )
    # 还没收到 fun 时是「未知」，不能拦
    assert session.controls_blocked is False
    assert session.controls_blocked_reason == ""

    session.status.apply_report({"print": {"fun": "100d122002fbd"}})
    assert session.controls_blocked is True
    assert "开发者模式" in session.controls_blocked_reason
    assert "Developer Mode" in session.controls_blocked_reason
    # 被挡住时 can_control 必须为假，否则界面仍会放开按钮
    assert session.can_control is False


def test_已开开发者模式时不拦控制():
    """契约：`fun` 里签名位为 0（已开 Developer Mode）时不得拦截控制。"""
    session = PrinterSession(PrinterInfo(ip="127.0.0.1", model=PrinterModel.A2L))
    session.status.apply_report({"print": {"fun": "100d102002fbd"}})
    assert session.controls_blocked is False
    assert session.controls_blocked_reason == ""


def test_老机型没有fun字段时控制不被误伤():
    """契约：没有 `fun` 字段的老机型（P1/A1/X1 等）不能被判成需要开发者模式。

    这是最容易写错的地方：若把「无法判断」当成「需要签名」，所有老机型的
    控制按钮都会被置灰，等于把已有功能弄坏。
    """
    for model in (PrinterModel.P1S, PrinterModel.A1, PrinterModel.X1C):
        session = PrinterSession(PrinterInfo(ip="127.0.0.1", model=model))
        session.status.apply_report({"print": {"mc_percent": 40, "gcode_state": "RUNNING"}})
        assert session.controls_blocked is False, f"{model.label} 被误判为需要签名"
        assert session.controls_blocked_reason == ""
