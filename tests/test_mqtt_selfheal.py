"""遥测（MQTT）自愈的回归测试。

## 为什么需要

`paho` 自己会重连（`loop_start` + `reconnect_delay_set`），但**它也会卡死**：
Android 上息屏/切网之后底层 socket 状态坏掉，paho 的退避重连一直失败却不再报错；
初始 connect 一直失败时它也不会重新走一遍 TLS 探测。

症状是「**画面正常·遥测断开**」时不时出现、而且**不再自愈**，用户只能手动点重连。
`PrinterSession._mqtt_watchdog_tick()` 就是兜住这种情况的：靠
`MqttWorker.connect_count` 有没有涨来判断 paho 是真卡死了还是在正常重连。
"""

from __future__ import annotations

import time

from app.bambu.models import PrinterInfo, PrinterModel
from app.bambu.mqtt_worker import MqttWorker
from app.bambu.printer import MQTT_STUCK_SECONDS, PrinterSession


class FakeWorker:
    """可精确控制 connect_count 的替身，用来驱动看门狗的判据。"""

    def __init__(self, connect_count: int = 0) -> None:
        self.connect_count = connect_count
        self.restarts = 0
        self.stopped = False

    def restart(self) -> None:
        self.restarts += 1
        self.connect_count += 1

    def stop(self) -> None:
        self.stopped = True


def make_session(**overrides) -> PrinterSession:
    info = PrinterInfo(
        ip=overrides.pop("ip", "192.168.1.50"),
        serial=overrides.pop("serial", "01P00A1234567"),
        name=overrides.pop("name", "测试机"),
        model=overrides.pop("model", PrinterModel.P1S),
        access_code=overrides.pop("access_code", "12345678"),
    )
    return PrinterSession(info)


def _stuck_worker(seen: int = 1, count: int = 1) -> FakeWorker:
    """造一个"连过一次、之后再没连上"的 worker —— 也就是卡死的形态。"""
    worker = FakeWorker(connect_count=count)
    worker.last_connected_at = time.time() - MQTT_STUCK_SECONDS - 10
    worker.first_attempt_at = time.time() - MQTT_STUCK_SECONDS - 10
    return worker


# --------------------------------------------------------------- 判据


def test_连接正常推进时不重建():
    """契约：connect_count 在涨 = paho 在正常重连，看门狗不得插手。"""
    session = make_session()
    session.running = True
    worker = FakeWorker(connect_count=5)
    worker.last_connected_at = time.time() - MQTT_STUCK_SECONDS - 10
    worker.first_attempt_at = worker.last_connected_at
    session._mqtt = worker
    session._mqtt_seen_connects = 1  # 上次看到 1，现在是 5 -> 涨过了

    session._mqtt_watchdog_tick()

    assert worker.restarts == 0, "paho 在正常重连时不该被重建"
    assert session._mqtt_seen_connects == 5, "基线应当抬到当前值，供下一轮比较"


def test_长时间没连上则重建():
    """**核心回归**：90 秒没有成功连接 -> 整条重建。"""
    session = make_session()
    session.running = True
    worker = _stuck_worker()
    session._mqtt = worker
    session._mqtt_seen_connects = worker.connect_count

    session._mqtt_watchdog_tick()

    assert worker.restarts == 1, "卡死时必须重建连接"
    assert session.warnings, "要让用户能看到发生过自愈（便于排查网络）"


def test_刚启动不久不重建():
    """契约：刚开始连接（还没到 90 秒）不能急着重建，否则会和首次连接打架。"""
    session = make_session()
    session.running = True
    worker = FakeWorker(connect_count=0)
    worker.last_connected_at = 0.0
    worker.first_attempt_at = time.time() - 5  # 才试了 5 秒
    session._mqtt = worker
    session._mqtt_seen_connects = 0

    session._mqtt_watchdog_tick()

    assert worker.restarts == 0, "首次连接还没超时，不该重建"


def test_重建过一次之后不再反复重建():
    """契约：重建后计数会涨，下一轮不该立刻又重建（避免抖动）。"""
    session = make_session()
    session.running = True
    worker = _stuck_worker()
    session._mqtt = worker
    session._mqtt_seen_connects = worker.connect_count

    session._mqtt_watchdog_tick()
    assert worker.restarts == 1
    session._mqtt_watchdog_tick()
    assert worker.restarts == 1, "重建后计数已推进，不该在同一条件下再次重建"


def test_未启动或没有worker时不动():
    session = make_session()
    session.running = True
    session._mqtt = None
    session._mqtt_watchdog_tick()  # 不该抛

    session._mqtt = _stuck_worker()
    session.running = False
    session._mqtt_watchdog_tick()
    assert session._mqtt.restarts == 0, "会话没在跑就不该动连接"


def test_重建抛异常不会把看门狗带走():
    """契约：自愈失败只记日志，看门狗线程必须活下去（否则再也没人救）。"""
    session = make_session()
    session.running = True

    class Boom(FakeWorker):
        def restart(self) -> None:
            raise OSError("模拟重建失败")

    worker = Boom(connect_count=1)
    worker.last_connected_at = time.time() - MQTT_STUCK_SECONDS - 10
    worker.first_attempt_at = worker.last_connected_at
    session._mqtt = worker
    session._mqtt_seen_connects = 1

    session._mqtt_watchdog_tick()  # 不该抛


# --------------------------------------------------------------- 连接过程兜底


def test_连接过程抛异常时状态变成可诊断而不是静默退出(monkeypatch):
    """**核心回归**：TLS 探测/建连抛异常时，必须留下状态与原因。

    以前 `_start_blocking` 的 TLS 探测之后没有异常保护，抛异常会让后台线程
    **静默退出且永不重试** —— 遥测永久停在断连状态。
    """
    states: list[tuple[str, str]] = []
    worker = MqttWorker(
        host="127.0.0.1",
        serial="01P00A1234567",
        access_code="12345678",
        on_state=lambda state, detail: states.append((state, detail)),
    )

    def boom(*args, **kwargs):
        raise OSError("模拟 TLS 探测失败")

    # 打桩点在 mqtt_worker 模块自己的 tlsutil 引用上
    import app.bambu.mqtt_worker as mqtt_module

    monkeypatch.setattr(mqtt_module.tlsutil, "select_context", boom)

    worker._start_blocking()

    assert worker.state == MqttWorker.STATE_OFFLINE, "出错后状态应当是 offline，而不是停在 connecting"
    assert any(s == MqttWorker.STATE_OFFLINE for s, _ in states), "状态变化要通知宿主"
    assert "出错" in worker.detail or "失败" in worker.detail, f"要带上原因，实际：{worker.detail!r}"


def test_restart会先停再起(monkeypatch):
    """契约：`restart()` = stop + start（整条重建）。"""
    worker = MqttWorker(host="127.0.0.1", serial="x", access_code="y")
    calls: list[str] = []
    monkeypatch.setattr(worker, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(worker, "start", lambda: calls.append("start"))

    worker.restart()

    assert calls == ["stop", "start"]


def test_连接成功会推进计数与时间戳():
    """契约：`connect_count` / `last_connected_at` 是看门狗的判据，必须可靠推进。"""
    worker = MqttWorker(host="127.0.0.1", serial="x", access_code="y")
    assert worker.connect_count == 0
    assert worker.last_connected_at == 0.0

    class _RC:
        value = 0

    worker._on_connect(None, None, None, _RC())

    assert worker.connect_count == 1
    assert worker.last_connected_at > 0
    assert worker.state == MqttWorker.STATE_ONLINE
