"""用**程序自己的代码路径**检查一台 Moonraker 设备（只读）。

用法::

    python tools/moonraker_session_check.py 192.168.31.55 [--port 7125] [--seconds 8]

与 `tools/moonraker_probe.py` 的区别：那个是裸 HTTP 探测，用的是自己写的请求；
这个走 ``create_session()`` → 真实的 ``MoonrakerAdapter`` → 界面视图
``display_status()``，也就是**用户实际跑的那条路**。

它只做两件事：查状态（``POST /printer/objects/query``）和取快照（GET）。
**不下发任何会动机器的指令**（暂停、移动、加热都不碰）；文件列表是只读的。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu.models import PrinterInfo  # noqa: E402
from app.core import create_session, display_status  # noqa: E402
from app.core.registry import FAMILY_MOONRAKER, MOONRAKER_FALLBACK_PORT  # noqa: E402
from tools._common import enable_utf8  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    enable_utf8()
    parser = argparse.ArgumentParser(description="用程序自己的代码路径检查一台 Moonraker 设备（只读）")
    parser.add_argument("host")
    parser.add_argument("--port", type=int, default=MOONRAKER_FALLBACK_PORT)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--seconds", type=float, default=8.0, help="等待画面与状态的时间")
    parser.add_argument("--camera-url", default="", help="手填摄像头地址（覆盖自动发现）")
    args = parser.parse_args(argv)

    info = PrinterInfo(
        ip=args.host,
        name="moonraker-check",
        family=FAMILY_MOONRAKER,
        port=args.port,
        api_key=args.api_key,
        camera_url=args.camera_url,
    )
    session = create_session(info)
    print(f"=== {args.host}:{args.port} → {type(session).__name__} ===")
    print(f"base_url={session.base_url}")
    session.start()
    deadline = time.time() + max(2.0, args.seconds)
    try:
        while time.time() < deadline:
            if session.snapshot().online and session.latest_frame()[1]:
                break
            time.sleep(0.1)

        status = display_status(session.snapshot())
        print("\n=== 状态（经界面视图 display_status） ===")
        print(f"  在线={status.mqtt_online}  画面在线={status.camera_online}  "
              f"状态={status.state_text}  进度={status.progress}%")
        print(f"  任务={status.subtask_name!r}  层={status.layer_num}/{status.total_layer_num}")
        print(f"  喷嘴={status.nozzle_temper:.1f}/{status.nozzle_target_temper:.0f}  "
              f"热床={status.bed_temper:.1f}/{status.bed_target_temper:.0f}")
        print(f"  剩余={status.remaining_text}  预计完成={status.finish_time_text}  "
              f"耗时字段={getattr(status, 'print_duration_minutes', '（没有）')}")
        print(f"  通道={session.video_backend}  帧率={session.camera_fps:.2f}fps")
        print(f"  画面状态={session.last_camera_state}  说明={session.last_camera_detail!r}")

        print("\n=== 摄像头（session.cameras()） ===")
        cameras = session.cameras() if hasattr(session, "cameras") else []
        if not cameras:
            print("  一个都没发现（Moonraker 的 webcams 列表是空的，也没手填 URL）")
        for camera in cameras:
            mark = "✓" if camera["available"] else "✗"
            print(f"  {mark} [{camera['index']}] {camera['name']} "
                  f"location={camera['location'] or '-'}")
            print(f"       快照 {camera['url']}")
            if camera["stream_url"]:
                print(f"       流   {camera['stream_url']}")
            if camera["detail"]:
                print(f"       最近失败：{camera['detail']}")

        print("\n=== 取帧实测 ===")
        for camera in cameras:
            seq, frame = session.latest_frame(int(camera["index"]))
            if frame:
                head = frame[:4].hex()
                # 判断放在 f-string 外面：b"\xff" 的反斜杠在 f-string 里是
                # 3.12 之前不允许的写法（安卓版跑 3.10，见 tests/test_contracts.py）
                kind = "JPEG" if frame.startswith(b"\xff\xd8") else "不是 JPEG！"
                print(
                    f"  ✓ [{camera['index']}] {camera['name']:12} {len(frame) / 1024:7.1f} KB  "
                    f"序号={seq}  头 4 字节={head}  {kind}"
                )
            else:
                print(f"  ✗ [{camera['index']}] {camera['name']:12} 没有拿到帧")

        if session.warnings:
            print("\n=== 告警 ===")
            for text in session.warnings:
                print(f"  {text}")
    finally:
        session.stop()

    seq, frame = session.latest_frame()
    print(f"\n会话已停止；最后一帧 {len(frame) / 1024 if frame else 0:.1f} KB")
    return 0 if frame else 1


if __name__ == "__main__":
    raise SystemExit(main())
