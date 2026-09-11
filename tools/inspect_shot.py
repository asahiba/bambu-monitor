"""开发用：分析界面截图的像素分布，确认画面、状态条、进度条确实渲染出来了。

用法：``python tools/inspect_shot.py _ui1.png``
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:  # Windows 控制台默认 GBK，输出 ✓/✗ 会报错
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

ACCENT = (0x25, 0xC2, 0xD6)  # 进度条/高亮色
OK_GREEN = (0x2E, 0xCC, 0x71)


def main(path: str) -> int:
    QGuiApplication.instance() or QGuiApplication([])
    image = QImage(path)
    if image.isNull():
        print(f"无法读取图片：{path}")
        return 1

    width, height = image.width(), image.height()
    accent_pixels = 0
    green_pixels = 0
    bright_pixels = 0
    dark_pixels = 0
    total = 0
    colors: set[int] = set()

    step = 2
    for y in range(0, height, step):
        for x in range(0, width, step):
            pixel = image.pixel(x, y)
            r = (pixel >> 16) & 0xFF
            g = (pixel >> 8) & 0xFF
            b = pixel & 0xFF
            total += 1
            colors.add(pixel & 0xFFFFFF)
            if abs(r - ACCENT[0]) < 26 and abs(g - ACCENT[1]) < 26 and abs(b - ACCENT[2]) < 26:
                accent_pixels += 1
            if abs(r - OK_GREEN[0]) < 26 and abs(g - OK_GREEN[1]) < 26 and abs(b - OK_GREEN[2]) < 26:
                green_pixels += 1
            if r > 200 and g > 200 and b > 200:
                bright_pixels += 1
            if r < 40 and g < 40 and b < 40:
                dark_pixels += 1

    print(f"图片尺寸: {width}x{height}")
    print(f"采样点: {total}")
    print(f"颜色种类: {len(colors)}（太少说明窗口空白）")
    print(f"高亮色(进度条)像素: {accent_pixels}")
    print(f"在线绿点像素: {green_pixels}")
    print(f"白色文字像素: {bright_pixels}")
    print(f"深色背景像素: {dark_pixels}")

    ok = len(colors) > 200 and bright_pixels > 200 and accent_pixels > 50
    print("判定:", "界面渲染正常 ✓" if ok else "疑似空白/未渲染 ✗")

    # 分格统计：确认每个画面格子里确实有视频内容（而不是黑屏）
    columns = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    rows = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    print(f"\n按 {columns}x{rows} 分格统计视频区中心颜色：")
    for row in range(rows):
        for column in range(columns):
            cell_w = width // columns
            cell_h = height // rows
            cx = column * cell_w + cell_w // 2
            cy = row * cell_h + cell_h // 3  # 偏上，避开下方状态条
            r_sum = g_sum = b_sum = 0
            samples = 0
            for dy in range(-40, 41, 8):
                for dx in range(-60, 61, 8):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < width and 0 <= y < height:
                        pixel = image.pixel(x, y)
                        r_sum += (pixel >> 16) & 0xFF
                        g_sum += (pixel >> 8) & 0xFF
                        b_sum += pixel & 0xFF
                        samples += 1
            r, g, b = r_sum // samples, g_sum // samples, b_sum // samples
            has_content = (r + g + b) > 45
            print(
                f"  格({row},{column}) 中心({cx},{cy}) 平均色 RGB({r},{g},{b}) "
                f"{'有画面 ✓' if has_content else '疑似黑屏 ✗'}"
            )

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "_ui1.png"))
