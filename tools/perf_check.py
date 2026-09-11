"""开发用：测量不同帧率设置下，真实打印机会话的 CPU 占用。

用法：``python tools/perf_check.py [每档秒数] [IP…]``
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

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
only_ips = set(sys.argv[2:])

config = AppConfig.load()
printers = [p for p in config.printers if p.access_code and (not only_ips or p.ip in only_ips)]
if not printers:
    print("配置里没有带访问代码的打印机")
    sys.exit(2)

print(f"测试对象：{[p.ip for p in printers]}（{len(printers)} 路）")
print(f"每档观察 {seconds:.0f} 秒，CPU 以「单核占用百分比」表示\n")

sessions = [PrinterSession(info) for info in printers]
for session in sessions:
    session.start()

print("等待连接建立…")
time.sleep(12)

for fps in (0, 4, 8, 15):
    for session in sessions:
        session.set_max_fps(fps)
    time.sleep(3)  # 让设置生效并稳定
    started_cpu = time.process_time()
    started_wall = time.time()
    time.sleep(seconds)
    cpu = time.process_time() - started_cpu
    wall = time.time() - started_wall
    stats = []
    for session in sessions:
        status = session.snapshot()
        seq, frame = session.latest_frame()
        stats.append(
            f"{session.info.model.label}:{'在线' if status.camera_online else '离线'}"
            f" {len(frame) // 1024 if frame else 0}KB {session.camera_fps:.1f}fps"
        )
    label = "不限帧率" if fps == 0 else f"上限 {fps}fps"
    print(f"{label:<12} CPU {cpu / wall * 100:5.1f}% 单核   {'  '.join(stats)}")

for session in sessions:
    session.stop()
