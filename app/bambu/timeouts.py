"""协议层的超时、间隔与等待时长（单位见各常量注释，默认**秒**）。

这些值是「网络行为参数」，不是业务参数：它们取决于打印机固件多快回包、
RTSPS 握手要多久、TLS 重协商会不会卡住，跟界面偏好无关。以前它们散落在
`camera.py` / `rtsp.py` / `mqtt_worker.py` / `probe.py` / `printer.py` /
`discovery.py` 与两个对话框里，同一类等待在不同文件里写着不同的数字，
既不好对比也不好调优（端口已经收敛到 ``ports.py``，这里是它的超时表兄弟）。

## 命名约定

* ``*_TIMEOUT``  单次操作的超时（连接、读一帧、等一个报文）；
* ``*_JOIN``     等线程收尾的上限（线程回收用，**不是**业务等待）；
* ``*_INTERVAL`` 循环里的轮询间隔或两次尝试之间的停顿；
* ``*_CAP``      「不要超过这个上限」；
* ``*_SLACK``    「在这个基础上再加一点」的余量。

## 想调优时先看这里

* 网络慢 / 打印机固件老 -> 先加大 ``CAMERA_TLS_TIMEOUT``、
  ``CAMERA_FRAME_HEADER_TIMEOUT``、``RTSP_OPEN_TIMEOUT_MS``；
* 界面「点了没反应」-> 看 ``PROBE_MQTT_TIMEOUT``（添加/诊断对话框等多久）；
* 关闭程序很慢 -> 看各个 ``*_JOIN``（它们决定最坏情况下要等多久）。

本模块**不导入任何其它 app 模块**（与 ``ports.py`` 同样的纪律），
所以协议层、界面、命令行都可以安全引用。
"""

from __future__ import annotations

# --------------------------------------------------------------------------- 遥测（MQTT 8883）
#: 单次 TLS + MQTT 连接握手的超时。实测局域网内通常 <1 秒，
#: 4 秒足以覆盖固件较慢的机型，又不会让「点测试连接」等太久。
MQTT_CONNECT_TIMEOUT = 4.0
#: 停止时等 MQTT 收尾线程的上限（paho 的 loop_stop 偶尔要等一下）。
MQTT_THREAD_JOIN = 5.0
#: 多久没有「连上过」就判定 paho 卡死并整条重建（见 printer.py 的看门狗）。
#: 必须明显大于 paho 自己的重连退避，否则会跟它打架。
MQTT_STUCK_SECONDS = 90.0

# --------------------------------------------------------------------------- 画面（6000 端口）
#: 6000 端口的 TLS 握手超时。X1 系列需要 SECLEVEL=0，重协商偶发偏慢。
CAMERA_TLS_TIMEOUT = 6.0
#: 读 socket 的单次切片。``_read_exact`` 每次最多等这么久就回头检查停止标志，
#: 这样「停止」最坏只延迟这么久生效（不能设太大）。
CAMERA_READ_SLICE = 5.0
#: 等一帧的 16 字节帧头的超时。取不到就判定流中断并重连。
CAMERA_FRAME_HEADER_TIMEOUT = 25.0
#: 等一整帧 JPEG 数据的超时（1080p 关键帧在慢网络下可能要几秒）。
CAMERA_FRAME_BODY_TIMEOUT = 30.0
#: 取首帧的总超时（探测/诊断里用；比单帧等待短，避免用户干等）。
CAMERA_FIRST_FRAME_TIMEOUT = 12.0
#: 停止 6000 通道时等线程收尾的上限。
CAMERA_STREAM_JOIN = 3.0
#: 连接失败后的首次退避，之后按 ``CAMERA_BACKOFF_FACTOR`` 递增到 ``CAMERA_BACKOFF_MAX``。
CAMERA_BACKOFF_START = 1.0
CAMERA_BACKOFF_FACTOR = 1.6
CAMERA_BACKOFF_MAX = 15.0
#: 鉴权失败（访问代码不对）后的退避。**不能太小**：口令错时高频重试只会
#: 反复被拒，而用户改完访问代码会走 `restart()` 立刻重建，不依赖这里的退避。
CAMERA_AUTH_BACKOFF = 5.0

# --------------------------------------------------------------------------- 画面（RTSPS 322）
#: FFmpeg/OpenCV 打开流的超时（毫秒，OpenCV 的参数单位）。
RTSP_OPEN_TIMEOUT_MS = 6000
#: ``RtspStream.wait_first_frame()`` 的默认超时。
RTSP_FIRST_FRAME_TIMEOUT = 10.0
#: 会话采纳一条 RTSPS 流之前等首帧的超时。打包版首次加载 cv2 可能很慢，给足。
RTSP_ADOPT_TIMEOUT = 20.0
#: 丢弃一条没取到画面的流时等它收尾的上限。
RTSP_STREAM_JOIN = 2.0
#: 两次 RTSPS 尝试之间的停顿（给打印机一点时间回收上一次连接）。
RTSP_RETRY_PAUSE = 4.0

# --------------------------------------------------------------------------- 会话/看门狗
#: ``stop()`` 里等「建立/切换视频通道」线程收尾的上限。
VIDEO_STOP_JOIN = 2.0
#: ``restart()`` 里等的上限：必须比 ``VIDEO_STOP_JOIN`` 宽，
#: 否则重连时上一轮的线程还在跑（RTSPS 取帧最长 20 秒）。
VIDEO_RESTART_JOIN = 5.0
#: 遥测自愈看门狗的检查间隔。
WATCHDOG_INTERVAL = 15.0

# --------------------------------------------------------------------------- 自动搜索
#: 停止搜索时等扫描线程收尾的上限。
DISCOVERY_THREAD_JOIN = 1.5
#: 搜索接收循环的轮询间隔（决定「停止」最坏延迟）。
DISCOVERY_POLL_INTERVAL = 0.1
#: 连续发送单播扫描包之间的间隔（太快会被打印机节流/丢弃）。
DISCOVERY_SEND_INTERVAL = 0.05

# --------------------------------------------------------------------------- 探测与诊断（界面）
#: 「添加打印机」对话框里「测试连接」等遥测的上限。
PROBE_MQTT_TIMEOUT = 10.0
#: 探测时等画面的超时上限（``min(CAP, timeout + SLACK)``：
#: 画面比遥测慢，但也不能因为用户把总超时调大就无限等）。
PROBE_CAMERA_TIMEOUT_CAP = 12.0
PROBE_CAMERA_TIMEOUT_SLACK = 4.0
#: 探测等遥测时轮询一次状态标志的间隔。
PROBE_POLL_INTERVAL = 0.2
#: 「通道诊断」的 TCP 端口连通性检查超时（三个端口各来一次，要短）。
DIAG_TCP_TIMEOUT = 3.0
#: 「通道诊断」里单独做一次 TLS 握手的超时。
DIAG_TLS_TIMEOUT = 4.0
#: 「通道诊断」里 RTSP DESCRIBE 的 TLS 连接超时与读响应超时。
DIAG_RTSP_TLS_TIMEOUT = 5.0
DIAG_RTSP_READ_TIMEOUT = 6.0
