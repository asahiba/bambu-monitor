"""开发用：复现「添加打印机 → 测试连接 → 立刻关闭对话框」的退出崩溃。

用法：``python -X faulthandler tools/add_dialog_close_probe.py``
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.add_dialog import PrinterEditDialog  # noqa: E402


def step(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


app = QApplication([])
dialog = PrinterEditDialog()
dialog.ip_edit.setText("127.0.0.1")
dialog.code_edit.setText("12345678")

thread = None
step("启动测试连接")
dialog._start_test()
thread = dialog._thread
step(f"线程已启动：{thread is not None}")

step("立刻关闭对话框")
dialog.close()

for index in range(20):
    alive = thread.isRunning() if thread is not None else False
    parent = thread.parent().__class__.__name__ if (thread and thread.parent()) else None
    step(f"  +{index * 0.25:.2f}s  线程运行中={alive}  父对象={parent}")
    if not alive:
        break
    app.processEvents()
    time.sleep(0.25)

step("脚本结束")
