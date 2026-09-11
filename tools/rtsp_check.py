"""开发用：验证 RTSPS(322) 通道在有/无访问权限时的行为（不阻塞、不误判）。

用法：``python tools/rtsp_check.py <IP> [访问代码|--auto]``
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu.rtsp import RtspStream  # noqa: E402
from tools._common import enable_utf8, resolve_code  # noqa: E402

enable_utf8()

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.31.27"
CODE = resolve_code(HOST, sys.argv[2] if len(sys.argv) > 2 else "")
if not CODE:
    print("没有可用的访问代码（可传 --auto 从配置读取，或直接给出）")
    sys.exit(2)

print(f"opencv 可用: {RtspStream.available()}")
states: list[tuple[str, str]] = []
stream = RtspStream(HOST, CODE, on_state=lambda s, d: states.append((s, d)), name="rtsp-check")
started = time.time()
stream.start()
frame = stream.wait_first_frame(7.0)
elapsed = time.time() - started
print(f"7 秒内拿到画面: {'是' if frame else '否'}（耗时 {elapsed:.1f}s，画面 {len(frame) if frame else 0} 字节）")
stream.stop()
stream.join(timeout=5)
print("状态序列:", states)
