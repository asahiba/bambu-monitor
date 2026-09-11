"""开发用：查看某台打印机状态报文里的关键原始字段（只读，不下发任何指令）。

用法：``python tools/raw_fields.py <IP>``
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu.printer import PrinterSession  # noqa: E402
from app.config import AppConfig  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

enable_utf8()

ip = sys.argv[1] if len(sys.argv) > 1 else ""
config = AppConfig.load()
info = next((item for item in config.printers if item.ip == ip), None)
if info is None:
    print(f"配置里没有 {ip}")
    sys.exit(2)

session = PrinterSession(info)
session.start()
deadline = time.time() + 20
while time.time() < deadline and not session.status.mqtt_online:
    time.sleep(0.3)
time.sleep(3)
session.stop()

raw = session.snapshot().raw
keys = [
    "gcode_state", "mc_percent", "mc_remaining_time", "nozzle_temper", "bed_temper",
    "chamber_temper", "wifi_signal", "lights_report", "hms", "print_error", "tray_now",
    "vt_tray", "ams", "spd_lvl", "layer_num", "total_layer_num",
]
print(f"{info.display_name()}（{info.model.label}）状态报文关键字段：")
for key in keys:
    value = raw.get(key)
    if key == "ams" and isinstance(value, dict):
        value = f"{len(value.get('ams', []))} 个 AMS 单元, tray_now={value.get('tray_now')}"
    if key == "vt_tray" and isinstance(value, dict):
        value = {k: value.get(k) for k in ("tray_type", "tray_color", "remain")}
    print(f"  {key:20} = {value!r}")
print(f"\n解析结果：仓温={session.snapshot().chamber_temper} 灯光={session.snapshot().lights} "
      f"AMS={len(session.snapshot().ams_trays)} 槽 外挂={session.snapshot().vt_tray is not None}")
print(f"机型是否显示仓温：{info.model.has_chamber_sensor}")

print("\n料卷明细（余量是否可信取决于有没有 RFID 标签）：")
trays = list(session.snapshot().ams_trays)
if session.snapshot().vt_tray is not None:
    trays.append(session.snapshot().vt_tray)
for tray in trays:
    raw_tray = None
    ams = raw.get("ams")
    if isinstance(ams, dict):
        for unit in ams.get("ams", []):
            for item in unit.get("tray", []):
                if str(item.get("id")) == str(tray.tray_id) and unit.get("id") == str(tray.ams_id):
                    raw_tray = item
    tag = (raw_tray or {}).get("tag_uid", "?") if raw_tray else "?"
    print(
        f"  {tray.label:8} 类型={tray.tray_type or '--':6} 颜色={tray.color_hex or '--':8} "
        f"remain={tray.remain:>4} RFID={'有' if tray.has_rfid else '无'} "
        f"→ 界面显示余量：{tray.remain_text or '不显示'}（{tray.remain_hint}）"
    )
