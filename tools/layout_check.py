"""开发用：离屏验证画面布局（顺序 + 重点画面跨格）的装箱结果。

用法：``python tools/layout_check.py``
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.bambu.models import PrinterInfo, PrinterModel  # noqa: E402
from app.config import AppConfig  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

enable_utf8()

app = QApplication(sys.argv)
config = AppConfig()
config.persist = False
config.auto_connect = False
models = [PrinterModel.X2D, PrinterModel.P1S, PrinterModel.A1, PrinterModel.X1C,
          PrinterModel.P1P, PrinterModel.A1MINI, PrinterModel.P2S]
config.printers = [
    PrinterInfo(ip=f"10.0.0.{index + 10}", name=f"P{index}", serial=f"TEST{index:011d}",
                model=models[index % len(models)], access_code="")
    for index in range(7)
]
window = MainWindow(config)
window.resize(1400, 900)

def report(title: str) -> None:
    positions = []
    for tile in window.tiles:
        index = window.grid.indexOf(tile)
        row, column, row_span, column_span = window.grid.getItemPosition(index)
        positions.append(
            f"{tile.session.info.display_name()}(span={tile.session.info.tile_span})"
            f"@{row},{column} {row_span}×{column_span}"
        )
    print(f"\n[{title}]")
    for line in positions:
        print("   ", line)

report("默认布局（全部 1 格）")

# 让 X2D（第一个）占大格
window.tiles[0].session.info.tile_span = 2
window.tiles[0].session.info.name = "X2D"
window.rebuild_grid()
report("X2D 设为重点画面（2×2）")

# 再验证顺序调整：把最后一路置顶
window.handle_layout_action(window.tiles[-1], "top")
report("把最后一路移到最前")

# 单画面模式
window.toggle_single_view(window.tiles[0])
report("单画面模式")
window.toggle_single_view(window.tiles[0])

# 恢复并切到固定 4 列
window.config.columns = 4
window.rebuild_grid()
report("固定 4 列 + 重点画面")

window.close()
app.quit()
print("\n布局检查完成")
