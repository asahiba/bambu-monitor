"""开发用：单独复现「网页监控开关」的启停流程，定位卡死点。

用法：``python tools/web_toggle_probe.py``
"""

from __future__ import annotations

import os
import socket
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.bambu.models import PrinterInfo, PrinterModel  # noqa: E402
from app.config import AppConfig  # noqa: E402


def step(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


app = QApplication([])
from app.ui.main_window import MainWindow  # noqa: E402

with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]

config = AppConfig()
config.persist = False
config.auto_connect = False
config.web_port = port
config.printers = [
    PrinterInfo(ip="127.0.0.10", name="冒烟机", serial="SMOKE0000000001",
                model=PrinterModel.P1S, access_code="12345678")
]

step("构造主窗口")
window = MainWindow(config)
step("启动网页服务")
window.toggle_web_server(True)
step(f"服务 running={window.web is not None and window.web.running}")
time.sleep(1.0)

step("关闭网页服务")
window.toggle_web_server(False)
step("关闭完成")

step("关闭主窗口")
window.close()
step("主窗口已关闭")
app.processEvents()
step("脚本结束")
