"""开发用：用错误的访问代码连真实打印机，验证错误提示路径（不会真的登录成功）。

用法：``python tools/auth_error_check.py 192.168.31.27``

退出码：``0`` = 「错误访问代码」这条路径被正确识别（画面/遥测任一侧报鉴权错误），
``1`` = 没看出鉴权被拒（说明提示路径有问题，或者打印机其实接受了这个口令）。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools._common import enable_utf8  # noqa: E402

enable_utf8()

from app.bambu.camera import CameraStream  # noqa: E402
from app.bambu.mqtt_worker import MqttWorker  # noqa: E402

#: 两侧都判定为鉴权失败的取值
AUTH_ERROR_STATES = {"auth_error"}


def main(argv: list[str]) -> int:
    host = argv[1] if len(argv) > 1 else "192.168.31.27"

    states: list[tuple[str, str]] = []
    camera = CameraStream(host, "00000000", on_state=lambda s, d: states.append((s, d)))
    camera.start()
    frame = camera.wait_first_frame(12.0)
    print(f"画面: frame={'有' if frame else '无'} state={camera.state} detail={camera.detail}")
    camera.stop()

    mqtt_states: list[tuple[str, str]] = []
    worker = MqttWorker(
        host, "", "00000000", on_state=lambda s, d: mqtt_states.append((s, d)), name="badcode"
    )
    worker.start()
    time.sleep(8)
    print(f"遥测: state={worker.state} detail={worker.detail}")
    worker.stop()
    print("相机状态序列:", states)
    print("遥测状态序列:", mqtt_states)

    camera_rejected = camera.state in AUTH_ERROR_STATES or any(
        state in AUTH_ERROR_STATES for state, _ in states
    )
    mqtt_rejected = worker.state in AUTH_ERROR_STATES or any(
        state in AUTH_ERROR_STATES for state, _ in mqtt_states
    )
    if camera_rejected or mqtt_rejected:
        print("\n结论：错误访问代码被识别为鉴权失败 ✓（这正是界面提示所依赖的路径）")
        return 0
    print(
        "\n结论：两侧都没有报鉴权失败 ✗ —— 要么打印机接受了这个口令（不该发生），"
        "要么「访问代码错误」的提示路径坏了（界面会一直显示「连接中」）。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
