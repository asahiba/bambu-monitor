"""开发用：离屏验证画面布局（顺序 + 重点画面跨格）的装箱结果。

.. deprecated::
    **已被 ``tools/layout_fit_check.py`` 取代**，请优先用后者：那个脚本会用真实
    字体度量算高度、检查也更细。本文件保留仅作历史对照。
    （它现在**也有退出码**了 —— 之前只打印，于是永远进不了 CI，
    连「画面重叠/丢格」这种明显问题都得靠人眼看输出。）

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

failures: list[str] = []

#: 单个画面允许的最大跨格。超过它一定是「读到了脏数据」，不是真的布局
MAX_SPAN = 8


def slots() -> list[tuple[str, int, int]]:
    """每个画面占的格子：``(名称, 行, 列)``，行列按跨格展开成单元格列表。

    ⚠️ 两个坑（本脚本第一版就踩了，还因此看起来像「脚本卡死」）：

    1. 单画面模式下，不在网格里的画面 ``indexOf()`` 返回 ``-1``，而
       ``getItemPosition(-1)`` 会返回**未初始化的内存**（实测拿到
       ``@-57146960,451 -54902140×451`` 这种值）—— 必须跳过 ``index < 0`` 的；
    2. 拿到脏数据后如果照着跨格值去 ``range()`` 展开，就是上亿次循环 →
       脚本半天不结束。所以跨格超出 :data:`MAX_SPAN` 时只记问题、不展开。
    """
    out: list[tuple[str, int, int]] = []
    for tile in window.tiles:
        name = tile.session.info.display_name()
        index = window.grid.indexOf(tile)
        if index < 0:
            # 单画面模式：这一路当前不在网格里，不算布局问题
            continue
        row, column, row_span, column_span = window.grid.getItemPosition(index)
        if not (1 <= row_span <= MAX_SPAN and 1 <= column_span <= MAX_SPAN):
            failures.append(
                f"跨格异常（读到脏数据？）：{name} row_span={row_span} column_span={column_span}"
            )
            continue
        for r in range(row, row + row_span):
            for c in range(column, column + column_span):
                out.append((name, r, c))
    return out


def report(title: str, expect_spans: list[int] | None = None) -> None:
    positions = []
    for tile in window.tiles:
        index = window.grid.indexOf(tile)
        if index < 0:
            positions.append(f"{tile.session.info.display_name()}(不在网格中：单画面模式)")
            continue
        row, column, row_span, column_span = window.grid.getItemPosition(index)
        positions.append(
            f"{tile.session.info.display_name()}(span={tile.session.info.tile_span})"
            f"@{row},{column} {row_span}×{column_span}"
        )
    print(f"\n[{title}]")
    for line in positions:
        print("   ", line)

    # 检查：画面之间不能重叠（重叠 = 有的画面被盖住，用户看到的正是「画面少了几路」）
    occupied: dict[tuple[int, int], str] = {}
    for name, row, column in slots():
        key = (row, column)
        if key in occupied:
            failures.append(f"[{title}] 画面重叠：{occupied[key]} 与 {name} 都占了 ({row},{column})")
        occupied[key] = name

    if expect_spans is not None:
        actual = []
        for tile in window.tiles:
            index = window.grid.indexOf(tile)
            if index < 0:
                continue
            _row, _column, row_span, column_span = window.grid.getItemPosition(index)
            if 1 <= row_span <= MAX_SPAN and 1 <= column_span <= MAX_SPAN:
                actual.append(row_span * column_span)
        if sorted(actual) != sorted(expect_spans):
            failures.append(f"[{title}] 占格不符：期望 {sorted(expect_spans)}，实际 {sorted(actual)}")


report("默认布局（全部 1 格）", expect_spans=[1] * len(window.tiles))

# 让 X2D（第一个）占大格
window.tiles[0].session.info.tile_span = 2
window.tiles[0].session.info.name = "X2D"
window.rebuild_grid()
report("X2D 设为重点画面（2×2）", expect_spans=[4] + [1] * (len(window.tiles) - 1))

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

print()
if failures:
    print(f"布局检查：发现问题 {len(failures)} 处 ✗")
    for line in failures:
        print("  -", line)
    sys.exit(1)
print("布局检查完成：全部通过 ✓")
sys.exit(0)
