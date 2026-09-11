"""开发用：离屏创建各对话框，确认能正常构建与跑搜索（不弹窗）。

用法：``python tools/dialog_smoke.py [观察秒数]``
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.config import AppConfig  # noqa: E402
from app.ui.add_dialog import PrinterEditDialog  # noqa: E402
from app.ui.discover_dialog import DiscoverDialog  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

enable_utf8()

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 32.0
app = QApplication(sys.argv)

edit = PrinterEditDialog()
print(f"PrinterEditDialog ok: title={edit.windowTitle()!r} stream_items={edit.stream_combo.count()}")
edit.close()

config = AppConfig.load()
dialog = DiscoverDialog(known=config.printers, timeout=config.last_timeout)
dialog.show()
print(f"DiscoverDialog 已创建（已保存 {len(config.printers)} 台，单轮 {config.last_timeout:.0f} 秒，持续搜索={dialog.continuous_box.isChecked()}）")


def snapshot(stage: str) -> None:
    rows = dialog.table.rowCount()
    checked = 0
    for row in range(rows):
        holder = dialog.table.cellWidget(row, 0)
        box = holder.findChild(type(dialog.continuous_box)) if holder is not None else None
        if box is not None and box.isChecked():
            checked += 1
    print(f"[{stage}] 列表 {rows} 行（已勾选 {checked}），状态栏：{dialog.status_label.text()}")


QTimer.singleShot(int(seconds * 1000 * 0.35), lambda: snapshot("中途"))
QTimer.singleShot(int(seconds * 1000), lambda: (snapshot("结束"), dialog.close(), app.quit()))
sys.exit(app.exec())
