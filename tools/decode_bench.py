"""开发用：对比两种 JPEG 解码路径的耗时（性能优化的直接证据）。

* 旧路径：整图解码（QImage.fromData）后再缩放到画面尺寸
* 新路径：QImageReader.setScaledSize 让 libjpeg 按比例解码

用法：``python tools/decode_bench.py [源图宽] [源图高] [目标宽] [目标高] [次数]``
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt  # noqa: E402
from PySide6.QtGui import QImage, QImageReader, QPainter  # noqa: E402

from tools._common import enable_utf8  # noqa: E402

enable_utf8()

source_w = int(sys.argv[1]) if len(sys.argv) > 1 else 1920
source_h = int(sys.argv[2]) if len(sys.argv) > 2 else 1080
target_w = int(sys.argv[3]) if len(sys.argv) > 3 else 480
target_h = int(sys.argv[4]) if len(sys.argv) > 4 else 270
rounds = int(sys.argv[5]) if len(sys.argv) > 5 else 30

# 造一张接近真实画面的 JPEG
image = QImage(source_w, source_h, QImage.Format_RGB32)
painter = QPainter(image)
for row in range(0, source_h, 8):
    painter.fillRect(0, row, source_w, 8, Qt.GlobalColor.darkGray if (row // 8) % 2 else Qt.GlobalColor.gray)
painter.fillRect(source_w // 4, source_h // 3, source_w // 3, source_h // 3, Qt.GlobalColor.lightGray)
painter.end()
data = QByteArray()
buffer = QBuffer(data)
buffer.open(QIODevice.WriteOnly)
image.save(buffer, "JPG", 80)
buffer.close()
jpeg = bytes(data.data())
print(f"源图 {source_w}x{source_h}，JPEG {len(jpeg) // 1024} KB；目标 {target_w}x{target_h}；各 {rounds} 次\n")


def decode_old(payload: bytes) -> QImage:
    full = QImage.fromData(payload, "JPG")
    return full.scaled(QSize(target_w, target_h), Qt.KeepAspectRatio, Qt.SmoothTransformation)


def decode_new(payload: bytes) -> QImage:
    source = QBuffer()
    source.setData(payload)
    source.open(QIODevice.ReadOnly)
    reader = QImageReader(source)
    size = reader.size()
    if size.isValid() and (size.width() > target_w or size.height() > target_h):
        reader.setScaledSize(size.scaled(QSize(target_w, target_h), Qt.KeepAspectRatio))
    result = reader.read()
    source.close()
    return result


for name, function in (("旧：整图解码 + 平滑缩放", decode_old), ("新：按目标尺寸解码", decode_new)):
    function(jpeg)  # 预热
    started = time.perf_counter()
    for _ in range(rounds):
        out = function(jpeg)
    elapsed = (time.perf_counter() - started) / rounds * 1000
    print(f"{name:<24} 单帧 {elapsed:6.2f} ms   输出 {out.width()}x{out.height()}")

print("\n折算：12 路画面按 8fps 刷新时，解码开销约为")
for name, function in (("旧", decode_old), ("新", decode_new)):
    started = time.perf_counter()
    for _ in range(rounds):
        function(jpeg)
    per_frame = (time.perf_counter() - started) / rounds
    print(f"  {name}路径：{per_frame * 12 * 8:.2f} 秒 CPU / 每秒（单核占比 {per_frame * 12 * 8 * 100:.0f}%）")
