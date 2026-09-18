"""只读验证：从真实打印机拉 RTSPS(322) 的 H.264，并把取到的码流落成文件。

用法：``python tools/rtsp_h264_check.py <IP> [访问代码|--auto] [秒数]``

为什么要这个工具：

* 安卓版看不到 RTSPS 机型画面的**根因**是"没解码器"，不是"连不上"。
  本工具用**纯 Python**（无 OpenCV）把码流取出来，证明这条路走得通；
* 同时把 AVCC 样本写成 `.h264`（Annex-B），可以用别的解码器验证码流是否完整 ——
  这正是网页端 WebCodecs 要吃的同一份数据；
* 全程只读：DESCRIBE / SETUP / PLAY + 收包，不发任何控制指令。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu.rtsp_h264 import RtspH264Client  # noqa: E402
from tools._common import enable_utf8, resolve_code  # noqa: E402

enable_utf8()

START_CODE = b"\x00\x00\x00\x01"


def avcc_to_annexb(data: bytes) -> bytes:
    """AVCC（4 字节长度前缀）→ Annex-B（起始码），便于用常见解码器验证。"""
    out = bytearray()
    index = 0
    while index + 4 <= len(data):
        size = int.from_bytes(data[index : index + 4], "big")
        index += 4
        if size <= 0 or index + size > len(data):
            break
        out += START_CODE + data[index : index + size]
        index += size
    return bytes(out)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    host = argv[0]
    code = resolve_code(host, argv[1] if len(argv) > 1 else "--auto")
    seconds = float(argv[2]) if len(argv) > 2 else 6.0
    if not code:
        print("没有可用的访问代码（可传 --auto 从配置读取）")
        return 2

    units: list = []
    states: list[tuple[str, str]] = []

    def on_unit(unit) -> None:
        units.append(unit)

    def on_state(state: str, detail: str) -> None:
        states.append((state, detail))
        print(f"  [{state}] {detail}")

    print(f"== 从 {host} 拉 RTSPS(322) 的 H.264（纯 Python，{seconds:.0f} 秒）==")
    client = RtspH264Client(host, code, on_access_unit=on_unit, on_state=on_state, name="check")
    client.start()
    try:
        got = client.wait_first_unit(timeout=min(10.0, seconds))
        print(f"  首帧：{'拿到' if got else '没拿到'}")
        deadline = time.time() + max(0.0, seconds - 2.0)
        while time.time() < deadline and not client._stop_event.is_set():
            time.sleep(0.2)
    finally:
        client.stop()
        client.join(timeout=3.0)

    params = client.parameters
    print("\n== 结果 ==")
    print(f"  访问单元（帧）：{len(units)}")
    keyframes = sum(1 for unit in units if unit.is_keyframe)
    print(f"  其中关键帧：{keyframes}")
    print(f"  RTP 包：{client.packets}，字节：{client.bytes_in / 1024:.0f} KB")
    print(f"  参数：codec={params.codec_string} sps={len(params.sps)}B pps={len(params.pps)}B "
          f"packetization={params.packetization_mode}")
    print(f"  avcC description：{params.avcc_description.hex()[:64]}…"
          f"（{len(params.avcc_description)} 字节）")
    if units:
        sizes = [unit.size for unit in units]
        print(f"  单帧字节：最小 {min(sizes)} / 最大 {max(sizes)} / 平均 {sum(sizes) // len(sizes)}")

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_rtsp_h264_sample.h264")
    with open(out, "wb") as handle:
        if params.sps:
            handle.write(START_CODE + params.sps)
        if params.pps:
            handle.write(START_CODE + params.pps)
        for unit in units:
            handle.write(avcc_to_annexb(unit.data))
    print(f"  已写出 Annex-B 样本：{out}（{os.path.getsize(out) / 1024:.0f} KB）")

    # 有 OpenCV 就顺手验证码流能不能解出画面（证明取到的是完整可解码的 H.264）
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
    print(f"  OpenCV 解码验证：解出 {decoded} 帧" + (f"，分辨率 {shape[1]}×{shape[0]}" if shape else ""))
    return 0 if decoded > 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
