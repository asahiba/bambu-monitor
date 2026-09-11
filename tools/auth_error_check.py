"""开发用：用错误的访问代码连真实打印机，验证错误提示路径（不会真的登录成功）。

用法：``python tools/auth_error_check.py 192.168.31.27``
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu.camera import CameraStream  # noqa: E402
from app.bambu.mqtt_worker import MqttWorker  # noqa: E402

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.31.27"

states: list[tuple[str, str]] = []
camera = CameraStream(HOST, "00000000", on_state=lambda s, d: states.append((s, d)))
camera.start()
frame = camera.wait_first_frame(12.0)
print(f"画面: frame={'有' if frame else '无'} state={camera.state} detail={camera.detail}")
camera.stop()

mqtt_states: list[tuple[str, str]] = []
worker = MqttWorker(
    HOST, "", "00000000", on_state=lambda s, d: mqtt_states.append((s, d)), name="badcode"
)
worker.start()
time.sleep(8)
print(f"遥测: state={worker.state} detail={worker.detail}")
worker.stop()
print("相机状态序列:", states)
print("遥测状态序列:", mqtt_states)
