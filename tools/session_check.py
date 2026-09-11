"""开发用：按配置里的打印机跑一遍完整会话（遥测 + 画面），真机验证通道选择。

用法：``python tools/session_check.py [秒数] [IP...]``
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

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
only_ips = set(sys.argv[2:])

config = AppConfig.load()
printers = [p for p in config.printers if p.access_code and (not only_ips or p.ip in only_ips)]
if not printers:
    print("配置里没有带访问代码的打印机")
    sys.exit(2)

print(f"将连接 {len(printers)} 台：{[p.ip for p in printers]}")
sessions = [PrinterSession(info) for info in printers]
for session in sessions:
    print(f"  启动 {session.info.display_name():14} {session.info.model.label:8} 期望通道={session.info.model.video_channel}")
    session.start()

deadline = time.time() + seconds
while time.time() < deadline:
    time.sleep(1.0)

print("\n" + "=" * 78)
ok = True
for session in sessions:
    status = session.snapshot()
    seq, frame = session.latest_frame()
    info = session.info
    print(f"{info.display_name():14} {info.model.label:8} 通道={session.video_channel:8} "
          f"遥测={'在线' if status.mqtt_online else '离线':4} 画面={'在线' if status.camera_online else '离线':4} "
          f"帧序号={seq} 帧大小={len(frame) // 1024 if frame else 0}KB "
          f"进度={status.progress}% 喷嘴={status.nozzle_temper:.0f} 热床={status.bed_temper:.0f}")
    print(f"    视频状态：{session.last_camera_state} / {session.last_camera_detail}")
    if session.warnings:
        print(f"    提示：{session.warnings}")
    if not (status.mqtt_online and status.camera_online):
        ok = False

for session in sessions:
    session.stop()

print("=" * 78)
print("结果：" + ("全部通道在线 ✓" if ok else "存在离线通道 ✗"))
sys.exit(0 if ok else 1)
