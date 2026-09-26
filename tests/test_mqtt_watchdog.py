"""遥测自愈看门狗的契约测试（``PrinterSession._mqtt_watchdog_tick``）。

## 这一版修的是什么

老判据是「``connect_count`` 超过 90 秒没涨就算卡死」。而**一条健康的长连接本来
就不会让计数上涨** —— 连上之后就一直是那一次。于是只要连接保持 90 秒以上，
每 90 秒就会被判一次「疑似卡死」并重建，用户看到的正是：

> 画面和打印信息一切正常，但一直提示遥测卡死；而真卡死时又迟迟不重连。

现在改成**主动探活**（发一次 ``pushall``，看设备有没有报文回来）：
在线且报文新鲜 -> 绝不重建；久无报文 -> 探活；探活无回应且超过阈值 -> 才重建。

这些用例都直接调 tick，用假 worker 控制"连接计数 / 最后连接时刻"，
用 `status.last_message_ts` 控制"报文新鲜度"，不碰真机与网络。
"""

from __future__ import annotations

import time

import pytest

from app.bambu.models import PrinterInfo, PrinterModel
from app.bambu.printer import PrinterSession
from app.bambu.timeouts import (
    MQTT_PROBE_SILENCE,
    MQTT_PROBE_TIMEOUT,
    MQTT_STUCK_SECONDS,
)


class FakeMqtt:
    """只实现看门狗用到的成员：连接计数、最后连接时刻、探活与重建。"""

    def __init__(self, connect_count: int = 1, last_connected_at: float = 0.0) -> None:
        self.connect_count = connect_count
        self.last_connected_at = last_connected_at or time.time()
        self.first_attempt_at = self.last_connected_at
        self.pushalls = 0
        self.restarts = 0

    def request_pushall(self, force: bool = False) -> None:
        self.pushalls += 1

    def restart(self) -> None:
        self.restarts += 1


def make_session(
    *,
    online: bool = True,
    message_age: float | None = 0.0,
    connected_age: float = 0.0,
) -> tuple[PrinterSession, FakeMqtt]:
    """造一个会话 + 假 worker。

    ``message_age=None`` 表示**从来没收到过报文**（``last_message_ts == 0``），
    与 ``0.0``（刚收到）是两回事 —— 这一点踩过一次。
    """
    session = PrinterSession(
        PrinterInfo(ip="192.168.31.110", name="看门狗测试机", model=PrinterModel.X2D)
    )
    session.running = True
    worker = FakeMqtt(last_connected_at=time.time() - connected_age)
    session._mqtt = worker  # noqa: SLF001 - 直接注入假 worker，测试看门狗本身
    session.status.mqtt_online = online
    session.status.last_message_ts = (
        0.0 if message_age is None else time.time() - message_age
    )
    return session, worker


# --------------------------------------------------------------- 不误报（核心）
def test_健康的长连接绝不被重建():
    """契约（这是以前的 bug）：在线且报文新鲜时，**永远不该重建**。

    老判据下这条必然失败：连接保持超过 90 秒、connect_count 一直等于 1，
    第 90 秒起每 15 秒就会重建一次，并往告警里塞一条"疑似卡死"。
    """
    session, worker = make_session(online=True, message_age=1.0, connected_age=600.0)
    for _ in range(10):
        session._mqtt_watchdog_tick()  # noqa: SLF001
    assert worker.restarts == 0, "健康连接被重建了（就是那个误报）"
    assert not [text for text in session.warnings if "卡死" in text or "无响应" in text]


def test_连接计数不涨也不算卡死只要在线且有报文():
    """契约：判据是"设备回不回话"，不是"连接计数涨不涨"。"""
    session, worker = make_session(online=True, message_age=0.5, connected_age=3600.0)
    session._mqtt_seen_connects = worker.connect_count  # noqa: SLF001 - 基线已抬平
    session._mqtt_watchdog_tick()  # noqa: SLF001
    assert worker.restarts == 0
    assert worker.pushalls == 0, "健康时不该去打扰设备"


# --------------------------------------------------------------- 真的卡死
def test_久无报文时先探活再重建():
    """契约：先探活（pushall）确认，确认无回应才重建。"""
    session, worker = make_session(
        online=True, message_age=MQTT_PROBE_SILENCE + 10, connected_age=MQTT_STUCK_SECONDS + 30
    )
    session._mqtt_watchdog_tick()  # noqa: SLF001
    assert worker.pushalls == 1, "疑似无响应时要先探活"
    assert worker.restarts == 0, "探活结果还没出来，不能马上重建"

    session._mqtt_watchdog_tick()  # noqa: SLF001 - 还没到探活超时
    assert worker.restarts == 0

    session._mqtt_probe_sent_at = time.time() - MQTT_PROBE_TIMEOUT - 1  # noqa: SLF001
    session._mqtt_watchdog_tick()  # noqa: SLF001
    assert worker.restarts == 1, "探活无回应 + 超过阈值就该重建"
    assert any("没有响应" in text for text in session.warnings), "要让用户看到真实原因"


def test_探活有回应就不重建():
    """契约：探活期间只要有报文回来，就认定连接是好的（只是刚才安静）。"""
    session, worker = make_session(
        online=False, message_age=MQTT_PROBE_SILENCE + 5, connected_age=MQTT_STUCK_SECONDS + 30
    )
    session._mqtt_watchdog_tick()  # noqa: SLF001 - 发出探活
    assert worker.pushalls == 1
    # 设备回话了
    session.status.last_message_ts = time.time()
    session._mqtt_probe_sent_at = time.time() - MQTT_PROBE_TIMEOUT - 1  # noqa: SLF001
    session._mqtt_watchdog_tick()  # noqa: SLF001
    assert worker.restarts == 0, "有回应还重建就是误报"
    assert session._mqtt_probe_sent_at == 0.0  # noqa: SLF001


def test_真卡死时不等满90秒就发现():
    """契约：判定"没反应"最多花 探活静默阈值 + 探活超时，不必等到 90 秒。"""
    session, worker = make_session(
        online=True, message_age=MQTT_PROBE_SILENCE + 1, connected_age=MQTT_STUCK_SECONDS + 1
    )
    session._mqtt_watchdog_tick()  # noqa: SLF001 - 探活
    session._mqtt_probe_sent_at = time.time() - MQTT_PROBE_TIMEOUT - 0.1  # noqa: SLF001
    session._mqtt_watchdog_tick()  # noqa: SLF001
    assert worker.restarts == 1
    assert MQTT_PROBE_SILENCE + MQTT_PROBE_TIMEOUT < MQTT_STUCK_SECONDS, (
        "探活路径必须比老的 90 秒判据更快发现真卡死"
    )


# --------------------------------------------------------------- 其它边界
def test_探活有限频():
    """契约：探活不能每 15 秒都发一次（P1 系列性能有限）。"""
    session, worker = make_session(
        online=False, message_age=MQTT_PROBE_SILENCE + 5, connected_age=10.0
    )
    # 手动画成"刚探活过、但还没到超时"
    session._mqtt_probe_request_at = time.time()  # noqa: SLF001
    session._mqtt_probe_sent_at = time.time()  # noqa: SLF001
    session._mqtt_watchdog_tick()  # noqa: SLF001
    assert worker.pushalls == 0


def test_连接计数涨过就抬基线():
    """契约：期间连上过（paho 自己重连成功）时更新基线。"""
    session, worker = make_session(online=False, message_age=MQTT_PROBE_SILENCE + 5)
    session._mqtt_seen_connects = 0  # noqa: SLF001
    worker.connect_count = 3
    session._mqtt_watchdog_tick()  # noqa: SLF001
    assert session._mqtt_seen_connects == 3  # noqa: SLF001


def test_没有worker或没在运行时什么都不做():
    """契约：会话没启动/没有遥测通道时，看门狗不该做任何事。"""
    session = PrinterSession(PrinterInfo(ip="127.0.0.1", model=PrinterModel.P1S))
    session._mqtt_watchdog_tick()  # noqa: SLF001 - 没有 worker
    session._mqtt = FakeMqtt()  # noqa: SLF001
    session.running = False
    session._mqtt_watchdog_tick()  # noqa: SLF001 - 没在运行
    assert session._mqtt.restarts == 0  # noqa: SLF001


@pytest.mark.parametrize("age", [0.0, 1.0, MQTT_PROBE_SILENCE - 1])
def test_报文新鲜度的边界(age):
    """契约：只要在静默阈值内，就不探活、不重建。"""
    session, worker = make_session(online=True, message_age=age, connected_age=1000.0)
    session._mqtt_watchdog_tick()  # noqa: SLF001
    assert worker.pushalls == 0 and worker.restarts == 0


def test_从没收到过报文时会去探活():
    """契约：刚开始连上但一封报文都没有，也要主动问问（而不是干等 90 秒）。"""
    session, worker = make_session(online=True, message_age=None, connected_age=5.0)
    session._mqtt_watchdog_tick()  # noqa: SLF001
    assert worker.pushalls == 1
    assert worker.restarts == 0
