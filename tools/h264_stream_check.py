"""端到端验证：把真实打印机的 RTSPS 码流按网页端要的格式推一遍。

用法：``python tools/h264_stream_check.py <IP> [访问代码|--auto] [秒数]``

它做的事（全程离线可跑，只有取流那一步连打印机）：

1. 起一个真实的 ``PrinterSession``（走纯 Python H.264 通路），
2. 用 ``WebServer._push_h264`` 的**同一段逻辑**把记录写成字节流，
3. 按网页端 `processBuffer` 的规则解析回去，断言：
   首条是初始化参数（codec 串 + avcC description），随后是关键帧/增量帧；
4. 把解析出来的 AVCC 访问单元还原成 Annex-B 落盘，用 OpenCV 解码验证 ——
   这一步等价于"网页端 WebCodecs 拿到的东西真的能解出画面"。

这样我们能在**没有安卓设备**的情况下，验证除"WebView 里画出来"之外的全部环节。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu.models import PrinterInfo, PrinterModel  # noqa: E402
from app.bambu.printer import PrinterSession  # noqa: E402
from tools._common import enable_utf8, resolve_code  # noqa: E402
from tools.rtsp_h264_check import START_CODE, avcc_to_annexb  # noqa: E402

enable_utf8()

MAGIC = b"BM"
HEADER = 9
KIND_H264 = 3


def write_record(buffer: bytearray, index: int, payload: bytes) -> None:
    """与 app/web/server.py 的 `_write_record` 完全同构。"""
    buffer += MAGIC + bytes([KIND_H264]) + index.to_bytes(2, "little") + len(payload).to_bytes(
        4, "little"
    ) + payload


def parse_records(buffer: bytes) -> list[tuple[int, bytes]]:
    """按网页端 `processBuffer` 的规则解析记录。"""
    out: list[tuple[int, bytes]] = []
    offset = 0
    while True:
        while offset + 1 < len(buffer) and not (
            buffer[offset] == MAGIC[0] and buffer[offset + 1] == MAGIC[1]
        ):
            offset += 1
        if offset + HEADER > len(buffer):
            break
        kind = buffer[offset + 2]
        index = buffer[offset + 3] | (buffer[offset + 4] << 8)
        length = int.from_bytes(buffer[offset + 5 : offset + 9], "little")
        if offset + HEADER + length > len(buffer):
            break
        payload = buffer[offset + HEADER : offset + HEADER + length]
        offset += HEADER + length
        if kind == KIND_H264:
            out.append((index, payload))
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    host = argv[0]
    code = resolve_code(host, argv[1] if len(argv) > 1 else "--auto")
    seconds = float(argv[2]) if len(argv) > 2 else 8.0
    if not code:
        print("没有可用的访问代码（可传 --auto 从配置读取）")
        return 2

    # 安卓版没有 OpenCV，所以这里**强制**关掉 OpenCV 通道，复现的正是安卓上的那条路。
    # 电脑上装了 OpenCV 时会走 RtspStream（服务端解码），验不到我们要验的东西。
    from app.bambu.rtsp import RtspStream

    RtspStream.available = staticmethod(lambda: False)  # type: ignore[method-assign]

    info = PrinterInfo(ip=host, access_code=code, name="h264-check", model=PrinterModel.X2D)
    session = PrinterSession(info)
    print(f"== 起会话（强制纯 Python H.264 通路，等价于安卓无 OpenCV）{host}，收 {seconds:.0f} 秒 ==")
    session.start()
    deadline = time.time() + seconds
    buffer = bytearray()
    sent_params: set[int] = set()
    try:
        while time.time() < deadline:
            params, units = session.latest_h264()
            if params and 0 not in sent_params:
                payload = json.dumps(
                    {
                        "codec": params["codec"],
                        "description": base64.b64encode(params["description"]).decode("ascii"),
                        "width": 0,
                        "height": 0,
                    }
                ).encode("utf-8")
                write_record(buffer, 0, b"\x01" + payload)
                sent_params.add(0)
            for unit in units:
                write_record(buffer, 0, (b"\x03" if unit.is_keyframe else b"\x02") + unit.data)
            time.sleep(0.05)
        print(f"  会话视频通道：{session.video_channel} / 模式 {session.video_mode}")
        print(f"  状态：{session.last_camera_state} —— {session.last_camera_detail}")
        if session.video_mode != "h264":
            print("  ✗ 没有走上 H.264 通路，本次没有验到目标路径")
    finally:
        session.stop()

    records = parse_records(bytes(buffer))
    print(f"\n== 按网页端规则解析回 {len(records)} 条 H.264 记录 ==")
    if not records:
        print("  ✗ 一条都没有：网页端会看到空画面")
        return 1
    index, first = records[0]
    assert index == 0
    if first[0] != 0x01:
        print(f"  ✗ 第一条不是初始化参数（首字节 {first[0]:#x}）")
        return 1
    params = json.loads(first[1:].decode("utf-8"))
    description = base64.b64decode(params["description"])
    print(f"  codec={params['codec']}  description={len(description)} 字节")
    frames = [item for _idx, item in records[1:]]
    keys = sum(1 for item in frames if item[0] == 0x03)
    deltas = sum(1 for item in frames if item[0] == 0x02)
    print(f"  访问单元：{len(frames)}（关键帧 {keys} / 增量帧 {deltas}）")

    out = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_h264_stream_sample.h264"
    )
    sps_len = int.from_bytes(description[6:8], "big")
    sps = description[8 : 8 + sps_len]
    pps_len = int.from_bytes(description[10 + sps_len : 12 + sps_len], "big")
    pps = description[12 + sps_len : 12 + sps_len + pps_len]
    with open(out, "wb") as handle:
        handle.write(START_CODE + sps + START_CODE + pps)
        for item in frames:
            handle.write(avcc_to_annexb(item[1:]))
    print(f"  已写出 Annex-B：{out}（{os.path.getsize(out) / 1024:.0f} KB）")

    try:
        import cv2
    except ImportError:
        print("  本机没有 OpenCV，跳过解码验证")
        return 0
    capture = cv2.VideoCapture(out)
    ok, frame = capture.read()
    decoded = 0
    while ok:
        decoded += 1
        ok, frame = capture.read()
    capture.release()
    shape = frame.shape if frame is not None else None
    print(f"  OpenCV 解码验证：解出 {decoded} 帧" + (f"，{shape[1]}×{shape[0]}" if shape else ""))
    if decoded <= 0:
        print("  ✗ 解不出画面：网页端的 WebCodecs 也会解不出来")
        return 1
    print("  ✓ 网页端拿到的那份数据确实是可解码的 H.264（只差 WebView 里画出来这一步）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
