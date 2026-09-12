"""后台帧解码器。

界面线程只负责贴图，JPEG 解码与缩放都放到本线程里完成：

* 12 路 1080p 画面按 8fps 刷新时，解码开销约为 100 次/秒，
  放在界面线程里会直接把界面拖卡；
* 用 ``QImageReader.setScaledSize`` 可以让 libjpeg 直接按比例解码（DCT 缩放），
  比「整图解码后再缩放」快得多。
"""

from __future__ import annotations

import threading
from typing import Optional, Tuple

from PySide6.QtCore import QBuffer, QIODevice, QSize, Qt
from PySide6.QtGui import QImage, QImageReader

from ..bambu.printer import PrinterSession


class FrameDecoder(threading.Thread):
    """把某一路画面的 JPEG 帧解码成按显示尺寸缩放好的 QImage。"""

    def __init__(self, session: PrinterSession, width: int = 640, height: int = 360) -> None:
        super().__init__(name=f"decode-{session.info.ip}", daemon=True)
        self.session = session
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._target = (max(64, width), max(36, height))
        self._last_seq = -1
        self._seq = 0
        self._image: Optional[QImage] = None
        self.decode_count = 0

    # ------------------------------------------------------------------ 对外接口
    def set_target_size(self, width: int, height: int) -> None:
        """画面控件尺寸变化时调用（只在实际变化时生效）。"""
        width = max(64, int(width))
        height = max(36, int(height))
        with self._lock:
            if (width, height) != self._target:
                self._target = (width, height)

    def latest(self) -> Tuple[int, Optional[QImage]]:
        with self._lock:
            return self._seq, self._image

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ 内部
    def _decode(self, jpeg: bytes, width: int, height: int) -> Optional[QImage]:
        buffer = QBuffer()
        buffer.setData(jpeg)
        if not buffer.open(QIODevice.ReadOnly):
            return None
        try:
            reader = QImageReader(buffer)
            reader.setAutoTransform(True)
            source = reader.size()
            if source.isValid() and source.width() > 0:
                # 只在缩小时设置目标尺寸，避免把画面放大
                if source.width() > width or source.height() > height:
                    scaled = source.scaled(QSize(width, height), Qt.KeepAspectRatio)
                    if scaled.isValid() and scaled.width() > 0:
                        reader.setScaledSize(scaled)
            image = reader.read()
            return None if image.isNull() else image
        finally:
            buffer.close()

    def run(self) -> None:
        import logging

        logger = logging.getLogger("bambu-monitor.decoder")
        while not self._stop.wait(0.02):
            seq, jpeg = self.session.latest_frame()
            if jpeg is None or seq == self._last_seq:
                continue
            self._last_seq = seq
            with self._lock:
                width, height = self._target
            try:
                image = self._decode(jpeg, width, height)
            except Exception:  # noqa: BLE001 - 解码失败不能拖垮整路画面
                logger.debug("解码失败", exc_info=True)
                image = None
            if image is None:
                continue
            with self._lock:
                self._image = image
                self._seq = seq
            self.decode_count += 1
