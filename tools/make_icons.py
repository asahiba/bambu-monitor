"""开发用：生成 PWA 图标并写入 app/web/icons.py（base64 内嵌，避免打包漏文件）。

用法：``python tools/make_icons.py``
"""

from __future__ import annotations

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tools._common import enable_utf8  # noqa: E402

enable_utf8()

QApplication.instance() or QApplication(sys.argv)

SIZES = (192, 512)


def render(size: int, maskable: bool = False) -> bytes:
    image = QImage(size, size, QImage.Format_RGB32)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    try:
        # 深色底 + 青色渐变，与软件主题一致
        gradient = QLinearGradient(0, 0, size, size)
        gradient.setColorAt(0.0, QColor("#101a1f"))
        gradient.setColorAt(1.0, QColor("#0d1114"))
        painter.fillRect(0, 0, size, size, gradient)

        accent = QColor("#25c2d6")
        # 显示器外框（maskable 时留出安全边距）
        margin = size * (0.26 if maskable else 0.18)
        frame = QRect(int(margin), int(margin * 0.78), int(size - margin * 2), int(size - margin * 1.7))
        painter.setPen(Qt.NoPen)
        painter.setBrush(accent)
        painter.drawRoundedRect(frame, size * 0.06, size * 0.06)

        # 屏幕内部
        inset = max(2, int(size * 0.035))
        screen = frame.adjusted(inset, inset, -inset, -inset)
        painter.setBrush(QColor("#0b1418"))
        painter.drawRoundedRect(screen, size * 0.03, size * 0.03)

        # 屏幕上的三格画面
        cell_gap = max(2, int(size * 0.02))
        cell_w = (screen.width() - cell_gap * 4) // 3
        cell_h = screen.height() - cell_gap * 2
        for index in range(3):
            x = screen.left() + cell_gap + index * (cell_w + cell_gap)
            painter.setBrush(accent if index == 0 else QColor("#1f4d57"))
            painter.drawRoundedRect(
                QRect(x, screen.top() + cell_gap, cell_w, cell_h), size * 0.02, size * 0.02
            )

        # 底座
        painter.setBrush(accent)
        stand_w = frame.width() * 0.34
        painter.drawRoundedRect(
            QRect(int(frame.center().x() - stand_w / 2), frame.bottom() - inset // 2,
                  int(stand_w), int(size * 0.05)),
            size * 0.01, size * 0.01,
        )

        # 右上角小圆点（在线指示）
        dot = size * (0.11 if maskable else 0.14)
        painter.setBrush(QColor("#2ecc71"))
        painter.drawEllipse(int(size - dot - margin * 0.6), int(margin * 0.4), int(dot), int(dot))

        if size >= 512:
            painter.setPen(QColor("#e6edf1"))
            painter.setFont(QFont("Microsoft YaHei UI", int(size * 0.075), QFont.Bold))
            painter.drawText(
                QRect(0, int(size * 0.72), size, int(size * 0.2)), Qt.AlignHCenter | Qt.AlignTop, "监控台"
            )
    finally:
        painter.end()

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data.data())


def main() -> int:
    entries = {}
    for size in SIZES:
        entries[f"ICON_{size}"] = render(size, maskable=False)
        entries[f"ICON_{size}_MASKABLE"] = render(size, maskable=True)
    favicon = render(64, maskable=False)

    target = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "web", "icons.py")
    lines = [
        '"""PWA 图标（由 tools/make_icons.py 生成，base64 内嵌避免打包漏文件）。"""',
        "",
        "import base64",
        "",
    ]
    for name, payload in entries.items():
        encoded = base64.b64encode(payload).decode("ascii")
        lines.append(f"{name}_B64 = (")
        for offset in range(0, len(encoded), 96):
            lines.append(f'    "{encoded[offset:offset + 96]}"')
        lines.append(")")
        lines.append("")
    encoded = base64.b64encode(favicon).decode("ascii")
    lines.append("FAVICON_B64 = (")
    for offset in range(0, len(encoded), 96):
        lines.append(f'    "{encoded[offset:offset + 96]}"')
    lines.append(")")
    lines.append("")
    lines.append("")
    lines.append("def icon_bytes(name: str) -> bytes:")
    lines.append('    """按名字取图标字节（如 icon-192.png）。"""')
    lines.append("    table = {")
    lines.append('        "icon-192.png": ICON_192_B64,')
    lines.append('        "icon-512.png": ICON_512_B64,')
    lines.append('        "icon-192-maskable.png": ICON_192_MASKABLE_B64,')
    lines.append('        "icon-512-maskable.png": ICON_512_MASKABLE_B64,')
    lines.append('        "favicon.ico": FAVICON_B64,')
    lines.append("    }")
    lines.append("    payload = table.get(name)")
    lines.append("    return base64.b64decode(payload) if payload else b\"\"")
    lines.append("")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))

    print(f"已生成 {target}（{os.path.getsize(target) // 1024} KB）")
    for name, payload in entries.items():
        print(f"  {name}: {len(payload) // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
