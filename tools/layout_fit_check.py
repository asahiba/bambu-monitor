"""开发用：验证监控墙不会超出窗口（画面必须自动适配窗口大小）。

复现「画面跑到屏幕外、要手动拖动」的问题：如果每个画面的**最小宽度**过大，
网格会被撑得比窗口还宽，只能靠横向滚动条。

默认使用**真实平台字体度量**（窗口用 WA_DontShowOnScreen 隐藏，不会弹出来打扰人），
因为中文界面下的字体宽度正是这个问题的主要变量。

用法：``python tools/layout_fit_check.py [画面数量] [--offscreen]``
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "--offscreen" in sys.argv:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.argv.remove("--offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QFontMetrics  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.bambu.models import PrinterInfo, PrinterModel  # noqa: E402
from app.config import AppConfig  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

enable_utf8()

count = int(next((a for a in sys.argv[1:] if a.isdigit()), 13))
app = QApplication(sys.argv)

config = AppConfig()
config.persist = False
config.auto_connect = False
models = [PrinterModel.X2D, PrinterModel.P1S, PrinterModel.A1, PrinterModel.X1C, PrinterModel.A1MINI]
config.printers = [
    PrinterInfo(
        ip=f"10.0.0.{index + 10}",
        name=f"打印机{index + 1}",
        serial=f"TESTSERIAL{index:04d}",
        model=models[index % len(models)],
        access_code="",
        tile_span=2 if index == 0 else 1,  # 第一台设为重点画面，顺便验证跨格
    )
    for index in range(count)
]

window = MainWindow(config)
# 用真实字体度量，但不把窗口显示出来
window.setAttribute(Qt.WA_DontShowOnScreen, True)
window.show()

metrics = QFontMetrics(window.font())
print(f"平台：{app.platformName()}   字体：{window.font().family()} {window.font().pointSize()}pt")
print(f"中文字宽参考：「打印中」= {metrics.horizontalAdvance('打印中')}px，"
      f"「喷嘴 245/250℃」= {metrics.horizontalAdvance('喷嘴 245/250℃')}px")
print(f"画面数量：{count}（第一台为重点画面 2×2）\n")

ok = True

for width, height in ((1920, 1009), (1600, 900), (1366, 768), (1280, 800), (1024, 700)):
    window.resize(width, height)
    app.processEvents()
    window.rebuild_grid()
    app.processEvents()

    viewport = window.scroll.viewport().size()
    grid_hint = window.grid_page.minimumSizeHint()
    bar = window.scroll.horizontalScrollBar()
    vertical = window.scroll.verticalScrollBar()
    visible = [tile for tile in window.tiles if tile.isVisible()]
    tile_min = window.tiles[0].minimumSizeHint() if window.tiles else None

    overflow = grid_hint.width() > viewport.width() + 2 or bar.maximum() > 0
    print(
        f"窗口 {width}×{height} → 视口 {viewport.width()}×{viewport.height()}  "
        f"网格最小宽 {grid_hint.width()}  横向滚动 {bar.maximum()}  纵向 {vertical.maximum()}  "
        f"可见画面 {len(visible)}"
    )
    if tile_min is not None:
        print(f"    单个画面最小尺寸 {tile_min.width()}×{tile_min.height()}")
    if overflow:
        ok = False
        print("    ✗ 横向超出窗口，画面会跑到屏幕外")
    else:
        # 顺便确认画面真的被摊开了（不是挤在角落里）
        if visible:
            rect = visible[0].geometry()
            print(f"    首个画面实际尺寸 {rect.width()}×{rect.height()}")
            if rect.width() < 150 or rect.height() < 100:
                ok = False
                print("    ✗ 画面被压得过小")
            else:
                print("    ✓ 横向完全适配窗口，画面已自动缩放")

window.close()

print("\n" + "=" * 62)
print("布局适配检查：" + ("全部通过 ✓" if ok else "存在超出窗口的情况 ✗"))
print("=" * 62)
sys.exit(0 if ok else 1)

