"""打印机局域网服务的端口与共享常量。

这些值以前硬编码散落在 `mqtt_worker.py` / `camera.py` / `rtsp.py` / `probe.py` /
`simulator.py` / UI 诊断对话框等 6 个以上文件里（同一端口要改好几处，容易漏）。
集中在这里之后，协议层与模拟器引用同一份定义，也便于文档与代码互相印证。

**关于自动搜索的常量**：`discovery.py` 里已经有一组收敛好的定义
（`SSDP_ADDR` / `SSDP_PORT` / `LEGACY_PORT` / `MAGIC` 等），并且在搜索流程内部
被频繁使用，所以**不搬到这里**，避免两处定义漂移；需要时从 `discovery` 导入即可。

本模块**不导入任何其它 app 模块**，因此可以安全地被协议层、模拟器、界面共同引用。
"""

from __future__ import annotations

#: MQTT over TLS 遥测端口（用户名 `bblp`，密码 = 局域网访问代码）
MQTT_PORT = 8883

#: 摄像头 JPEG 帧流端口（TLS + 80 字节鉴权包；A1/P1 系列只有这一条通道）
CAMERA_PORT = 6000

#: RTSPS 端口（X1/P2S/H2/X2D 系列；需在打印机上开启「局域网实时画面」）
RTSP_PORT = 322

#: 内置模拟器与各诊断脚本默认使用的访问代码。
#: 真机上这是打印机屏幕上显示的 8 位「局域网访问代码」。
DEFAULT_ACCESS_CODE = "12345678"
