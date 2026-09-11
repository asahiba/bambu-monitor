"""一站式诊断某台打印机的遥测与视频通道。

用法：``python tools/diagnose.py <IP> [访问代码|--auto]``
（``--auto`` 表示从 %APPDATA%\\BambuMonitor\\config.json 读取该 IP 已保存的访问代码）
"""

from __future__ import annotations

import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu import tlsutil  # noqa: E402
from app.bambu.camera import CameraStream  # noqa: E402
from app.bambu.models import detect_model  # noqa: E402
from app.bambu.probe import probe_printer  # noqa: E402
from app.bambu.rtsp import RtspStream  # noqa: E402
from tools._common import enable_utf8, lookup_serial, resolve_code  # noqa: E402
from tools.rtsp_describe import describe  # noqa: E402

enable_utf8()


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    host = argv[0]
    code = resolve_code(host, argv[1] if len(argv) > 1 else "")
    serial = lookup_serial(host)
    if not code:
        print("✗ 没有可用的访问代码（--auto 也读不到），请先给出或先关闭一次软件以保存配置")
        return 2

    print("=" * 70)
    print(f"诊断 {host}（访问代码 {len(code)} 位，序列号 {serial or '未知'}）")
    print("=" * 70)

    print("\n① 端口连通性")
    for port, name in ((8883, "MQTT 遥测"), (6000, "JPEG 画面"), (322, "RTSPS 画面")):
        try:
            with socket.create_connection((host, port), timeout=3):
                print(f"   {port:>5} {name}: 可连接 ✓")
        except OSError as exc:
            print(f"   {port:>5} {name}: 不可达 ✗ ({exc})")

    print("\n② TLS 参数（证书链 + 安全级别）")
    for port in (8883, 6000, 322):
        try:
            sock, verified = tlsutil.connect_tls(host, port, timeout=4.0)
            sock.close()
            print(f"   {port:>5}: 握手成功 ✓ 证书链{'已校验' if verified else '未校验'}")
        except Exception as exc:  # noqa: BLE001
            print(f"   {port:>5}: 握手失败 ✗ {exc.__class__.__name__}: {exc}")

    print("\n③ 6000 端口画面（TLS + 80 字节鉴权包 + JPEG 帧）")
    started = time.time()
    camera = CameraStream(host, code, serial=serial)
    camera.start()
    frame = camera.wait_first_frame(12.0)
    elapsed_camera = time.time() - started
    camera.stop()
    if frame:
        print(f"   成功 ✓ 取得 {len(frame) // 1024} KB 画面（{elapsed_camera:.1f}s）")
    else:
        print(f"   失败 ✗ 状态={camera.state} 说明={camera.detail}（{elapsed_camera:.1f}s）")

    print("\n④ RTSPS(322) DESCRIBE")
    describe(host, code)

    print("\n⑤ RTSPS(322) 实际拉流（OpenCV/FFmpeg）")
    if RtspStream.available():
        started = time.time()
        stream = RtspStream(host, code, name="diag")
        stream.start()
        result = stream.wait_first_frame(20.0)
        elapsed_rtsp = time.time() - started
        stream.stop()
        if result:
            print(
                f"   成功 ✓ 取得 {len(result) // 1024} KB 帧（{elapsed_rtsp:.1f}s），"
                f"路径 {stream.active_path}"
            )
        else:
            print(f"   失败 ✗ 状态={stream.state} 说明={stream.detail}（{elapsed_rtsp:.1f}s）")
    else:
        print("   跳过（未安装 opencv-python）")

    print("\n⑥ MQTT 遥测")
    probe = probe_printer(host, code, serial=serial, timeout=10.0, check_camera=False)
    for line in probe.summary().splitlines():
        print("   " + line)
    print(f"\n机型推断：{detect_model(serial)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
