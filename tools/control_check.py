"""开发用：验证控制指令与新增状态字段（**只在模拟器上测试**）。

**重要**：暂停 / 停止这类指令绝不会对真实打印机测试。
本脚本用内置模拟器验证 MQTT 指令格式与状态回读是否闭环。

覆盖：AMS 耗材颜色与余量、外挂料盘、HMS 文案、WiFi 信号、仓温、预计完成时间、
开关灯、暂停 / 继续 / 停止。

用法：``python tools/control_check.py``
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu.models import PrinterInfo  # noqa: E402
from app.bambu.printer import PrinterSession  # noqa: E402
from app.sim.simulator import start_simulator, stop_simulator  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

enable_utf8()

CODE = "12345678"
ok = True


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok
    print(f"   {'✓' if condition else '✗'} {label}{('：' + detail) if detail else ''}")
    if not condition:
        ok = False


print("① 启动 2 台模拟打印机")
printers, responder = start_simulator(2, CODE)
target = printers[0]
session = PrinterSession(
    PrinterInfo(
        ip=target.ip,
        serial=target.serial,
        name=target.name,
        access_code=CODE,
        stream_mode="tcp6000",
    )
)
session.start()

deadline = time.time() + 20
while time.time() < deadline:
    if session.snapshot().mqtt_online:
        break
    time.sleep(0.3)
status = session.snapshot()
check("遥测已连接", status.mqtt_online, session.last_camera_detail)

print("\n② 新增状态字段")
check("AMS 存在", status.ams_exist and len(status.ams_trays) == 4, f"{len(status.ams_trays)} 个槽位")
for tray in status.ams_trays:
    print(f"      {tray.label}: {tray.summary}  色块={tray.color_hex} 余量={tray.remain_text}")
check("AMS 有颜色与余量", all(t.color_hex and t.remain >= 0 for t in status.ams_trays))
check("外挂料盘", status.vt_tray is not None and bool(status.vt_tray.color_hex),
      status.vt_tray.summary if status.vt_tray else "无")
check("当前料卷可解析", status.active_tray is not None,
      status.active_tray.label if status.active_tray else "无")
check("WiFi 信号", status.wifi_signal.endswith("dBm"), f"{status.wifi_signal}（{status.wifi_level} 格）")
check("仓温", status.chamber_temper is not None and status.chamber_temper > 0,
      f"{status.chamber_temper}℃")
check("预计完成时间", status.finish_time_text != "--",
      f"{status.finish_time_text}（剩余 {status.remaining_text}）")
check("灯光状态", status.light_on is not None, f"舱灯={'开' if status.light_on else '关'}")

print("\n③ HMS 文案（模拟器最后一台带一条提示）")
hms_session = PrinterSession(
    PrinterInfo(
        ip=printers[-1].ip,
        serial=printers[-1].serial,
        name=printers[-1].name,
        access_code=CODE,
        stream_mode="tcp6000",
    )
)
hms_session.start()
deadline = time.time() + 20
while time.time() < deadline:
    if hms_session.snapshot().hms_items:
        break
    time.sleep(0.3)
hms_status = hms_session.snapshot()
check("收到 HMS 条目", bool(hms_status.hms_items), f"{len(hms_status.hms_items)} 条")
for item in hms_status.hms_items:
    print(f"      {item.display}")
    print(f"      说明页：{item.wiki}")
check("HMS 中文文案已解析", all(item.text for item in hms_status.hms_items))

print("\n④ 控制指令（仅模拟器）")


def wait_state(predicate, timeout: float = 12.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate(session.snapshot()):
            return True
        time.sleep(0.3)
    return False


check("关灯指令已下发", session.set_light(False))
check("模拟器舱灯已关闭", wait_state(lambda s: s.light_on is False), f"舱灯={session.snapshot().light_on}")
check("开灯指令已下发", session.set_light(True))
check("模拟器舱灯已打开", wait_state(lambda s: s.light_on is True), f"舱灯={session.snapshot().light_on}")

check("暂停指令已下发", session.pause_print())
check("状态变为已暂停", wait_state(lambda s: s.is_paused), session.snapshot().state_text)
check("继续指令已下发", session.resume_print())
check("状态恢复打印中", wait_state(lambda s: not s.is_paused and s.is_printing), session.snapshot().state_text)
check("停止指令已下发", session.stop_print())
check("状态变为空闲且进度归零", wait_state(lambda s: s.gcode_state.upper() == "IDLE" and s.progress == 0),
      f"{session.snapshot().state_text} {session.snapshot().progress}%")

print("\n⑤ 模拟器收到的指令原文（验证 MQTT 报文格式）")
for payload in target.received_commands:
    print(f"      {payload}")

expected = [
    ("system", "ledctrl", "off"),
    ("system", "ledctrl", "on"),
    ("print", "pause", None),
    ("print", "resume", None),
    ("print", "stop", None),
]
for section, command, mode in expected:
    found = False
    for payload in target.received_commands:
        body = payload.get(section)
        if isinstance(body, dict) and body.get("command") == command:
            if mode is None or body.get("led_mode") == mode:
                found = True
                break
    check(f"已收到 {section}.{command}{('/' + mode) if mode else ''}", found)

session.stop()
hms_session.stop()
stop_simulator(printers, responder)

print("\n" + "=" * 64)
print("控制与状态字段检查：" + ("全部通过 ✓" if ok else "存在失败项 ✗"))
print("=" * 64)
sys.exit(0 if ok else 1)
