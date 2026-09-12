"""视频显示控件：等比缩放 + 监控风格的叠加文字。"""

from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import theme


class VideoWidget(QWidget):
    """显示 JPEG 画面的控件。

    画面等比居中显示（两侧留黑边），左上角显示设备名/IP，
    右上角显示连接状态，右下角显示时间戳，与常见监控软件一致。
    """

    double_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # 最小值放小一些：多路画面时要能自动缩小塞进窗口，而不是撑出屏幕
        self.setMinimumSize(160, 90)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAutoFillBackground(False)
        self._image: Optional[QImage] = None
        self._scaled: Optional[QPixmap] = None
        self._title = ""
        self._subtitle = ""
        self._status_text = "等待连接"
        self._status_color = theme.TEXT_DIM
        self._placeholder = "无画面"
        self.show_timestamp = True
        self.show_overlay = True
        self._last_frame_ts = 0.0
        self._fps = 0.0

        self._font_small = QFont(theme.FONT_FAMILY, 8)
        self._font_title = QFont(theme.FONT_FAMILY, 9)
        self._font_title.setBold(True)
        self._font_clock = QFont("Consolas", 9)

    # ------------------------------------------------------------------ 数据
    def set_title(self, title: str, subtitle: str = "") -> None:
        if title == self._title and subtitle == self._subtitle:
            return
        self._title = title
        self._subtitle = subtitle
        self.update()

    def set_status(self, text: str, color: str) -> None:
        if text == self._status_text and color == self._status_color:
            return
        self._status_text = text
        self._status_color = color
        self.update()

    def set_frame(self, jpeg: bytes) -> None:
        image = QImage.fromData(jpeg, "JPG")
        if image.isNull():
            return
        self.set_image(image)

    def set_image(self, image: QImage) -> None:
        """直接接收已解码好的 QImage（解码在后台线程完成）。"""
        if image is None or image.isNull():
            return
        self._image = image
        self._scaled = None
        self._last_frame_ts = time.time()
        self.update()

    def clear_frame(self) -> None:
        self._image = None
        self._scaled = None
        self.update()

    def set_fps(self, fps: float) -> None:
        self._fps = fps

    def has_frame(self) -> bool:
        return self._image is not None

    @property
    def last_frame_age(self) -> float:
        if not self._last_frame_ts:
            return 1e9
        return time.time() - self._last_frame_ts

    # ------------------------------------------------------------------ 事件
    def resizeEvent(self, event) -> None:  # noqa: N802
        self._scaled = None
        super().resizeEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)

    def _ensure_scaled(self) -> None:
        if self._image is None:
            self._scaled = None
            return
        if self._scaled is not None:
            return
        # 后台解码器已按显示尺寸缩放，这里通常不需要再缩放
        if (
            abs(self._image.width() - self.width()) <= 8
            and abs(self._image.height() - self.height()) <= 8
        ):
            self._scaled = None
            return
        self._scaled = QPixmap.fromImage(self._image).scaled(
            self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
        )

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#05090b"))

        image = self._image
        if image is not None and not image.isNull():
            # 后台解码器已按控件尺寸解码，绝大多数情况这里是 1:1 贴图；
            # 尺寸不匹配时才生成一次缓存 pixmap，避免每帧重复平滑缩放。
            self._ensure_scaled()
            if self._scaled is not None and not self._scaled.isNull():
                x = (self.width() - self._scaled.width()) // 2
                y = (self.height() - self._scaled.height()) // 2
                painter.drawPixmap(x, y, self._scaled)
            else:
                area = self.rect()
                image_width = max(1, image.width())
                image_height = max(1, image.height())
                scale = min(area.width() / image_width, area.height() / image_height)
                width = max(1, int(image_width * scale))
                height = max(1, int(image_height * scale))
                painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
                painter.drawImage(
                    QRect((area.width() - width) // 2, (area.height() - height) // 2, width, height),
                    image,
                )
        else:
            painter.setPen(QColor(theme.TEXT_DIM))
            painter.setFont(QFont(theme.FONT_FAMILY, 12))
            painter.drawText(self.rect(), Qt.AlignCenter, self._placeholder)

        if self.show_overlay:
            self._paint_overlay(painter)
        painter.end()

    def _paint_overlay(self, painter: QPainter) -> None:
        # 左上角：设备名称 + IP
        painter.setFont(self._font_title)
        metrics = painter.fontMetrics()
        title = metrics.elidedText(self._title, Qt.ElideRight, max(60, self.width() - 150))
        text_width = metrics.horizontalAdvance(title)
        painter.fillRect(QRect(8, 6, text_width + 16, 20), QColor(0, 0, 0, 130))
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(QRect(16, 6, text_width, 20), Qt.AlignVCenter | Qt.AlignLeft, title)

        if self._subtitle:
            painter.setFont(self._font_small)
            sub_metrics = painter.fontMetrics()
            sub = sub_metrics.elidedText(self._subtitle, Qt.ElideRight, max(60, self.width() - 150))
            width = sub_metrics.horizontalAdvance(sub)
            painter.fillRect(QRect(8, 27, width + 16, 17), QColor(0, 0, 0, 110))
            painter.setPen(QColor(theme.TEXT_DIM))
            painter.drawText(QRect(16, 27, width, 17), Qt.AlignVCenter | Qt.AlignLeft, sub)

        # 右上角：连接状态
        painter.setFont(self._font_small)
        status_metrics = painter.fontMetrics()
        status_width = status_metrics.horizontalAdvance(self._status_text)
        box = QRect(self.width() - status_width - 34, 6, status_width + 26, 20)
        painter.fillRect(box, QColor(0, 0, 0, 140))
        painter.setBrush(QColor(self._status_color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(box.left() + 8, box.top() + 7, 7, 7)
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(
            QRect(box.left() + 20, box.top(), status_width + 4, box.height()),
            Qt.AlignVCenter | Qt.AlignLeft,
            self._status_text,
        )

        # 右下角：时间戳 / 帧率
        if self.show_timestamp:
            painter.setFont(self._font_clock)
            clock = time.strftime("%Y-%m-%d %H:%M:%S")
            if self._fps > 0:
                clock = f"{clock}  {self._fps:.0f}fps"
            clock_metrics = painter.fontMetrics()
            width = clock_metrics.horizontalAdvance(clock)
            rect = QRect(self.width() - width - 16, self.height() - 24, width + 8, 18)
            painter.fillRect(rect, QColor(0, 0, 0, 140))
            painter.setPen(QColor("#d8e6ec"))
            painter.drawText(rect, Qt.AlignVCenter | Qt.AlignRight, clock)

        # 无信号提示
        if self._image is None:
            painter.setFont(QFont(theme.FONT_FAMILY, 9))
            text = self._status_text or "无信号"
            rect = QRect(0, self.height() - 46, self.width(), 20)
            painter.setPen(QPen(QColor(theme.ERROR)))
            painter.drawText(rect, Qt.AlignCenter, f"● {text}")
