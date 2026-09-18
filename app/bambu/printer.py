"""把遥测（MQTT）与视频（6000 端口 / RTSPS）聚合成一台打印机的监控会话。"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from .camera import CameraStream
from .models import PrinterInfo, PrinterStatus
from .mqtt_worker import MqttWorker
from .timeouts import (
    MQTT_STUCK_SECONDS,
    RTSP_ADOPT_TIMEOUT,
    RTSP_RETRY_PAUSE,
    RTSP_STREAM_JOIN,
    VIDEO_RESTART_JOIN,
    VIDEO_STOP_JOIN,
    WATCHDOG_INTERVAL,
)

if TYPE_CHECKING:  # 仅类型标注：运行时按需在属性里导入，避免与 app.core 形成环
    from ..core.capabilities import DeviceCapabilities

LOGGER = logging.getLogger("bambu-monitor.session")

#: 遥测「卡死」判据：这么久没有成功连接过，就认为 paho 的重连已经卡住，
#: 整条重建（真正的定义在 `app/bambu/timeouts.py`，这里导入即转出，
#: 兼容既有调用方 —— 测试从本模块导入它）。



class PrinterSession:
    """一台打印机的完整监控会话（遥测 + 视频）。

    本类不依赖 GUI：界面通过 ``snapshot()`` 轮询状态，通过
    ``latest_frame()`` 取最新一帧，因此不会因为高频信号导致队列堆积。
    """

    #: 受「固件要求 MQTT 命令签名」影响的命令（归一化名）。
    #:
    #: 依据：官方 MQTT 签名机制的覆盖范围是**顶层带 ``print`` 的报文**
    #: （"Firmware rejects unsigned ``print`` commands when Developer Mode is off"）。
    #: `system` 段的灯控不在其中 —— 实测确认：同一台 A2L 在
    #: ``needs_mqtt_signature=True`` 时，`system/ledctrl`（灯）**生效**，
    #: 而 `print/pause` 与 `print/print_speed` 被设备忽略。
    SIGNATURE_SENSITIVE_COMMANDS = frozenset({"pause", "resume", "stop", "speed"})

    def __init__(self, info: PrinterInfo, on_event: Optional[Callable[[str], None]] = None) -> None:
        self.info = info
        self.status = PrinterStatus()
        self._on_event = on_event
        self._mqtt: Optional[MqttWorker] = None
        self._camera: Optional[CameraStream] = None
        self._rtsp: Any = None
        self._lock = threading.Lock()
        self.running = False
        self.last_camera_state = CameraStream.STATE_STOPPED
        self.last_camera_detail = ""
        self.mqtt_auth_error = False
        self.warnings: list[str] = []
        self.video_channel = "tcp6000"
        self._stream_lock = threading.Lock()
        #: 串行化视频通道的建立（看门狗切换与首帧建立不能同时进行）
        self._video_setup_lock = threading.Lock()
        #: 仍在运行的「建立视频通道」线程；stop() 必须能等到它们结束
        self._video_threads: list[threading.Thread] = []
        self._watchdog: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()
        #: 遥测自愈用：上一次看到的 MqttWorker.connect_count。
        #: 看门狗靠它判断"这段时间里 paho 到底连上过没有"，见 _mqtt_watchdog_tick。
        self._mqtt_seen_connects = 0

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.warnings.clear()
        self._start_telemetry()
        if self.info.access_code:
            # 视频通道（尤其 RTSPS 探测）可能阻塞数秒，放到后台线程，避免卡住界面
            self._spawn_video_thread(self._start_camera, "video-setup")
            self._watchdog_stop.clear()
            self._watchdog = threading.Thread(
                target=self._video_watchdog, name=f"video-watch-{self.info.ip}", daemon=True
            )
            self._watchdog.start()
        else:
            self.warnings.append("未填写访问代码，无法显示画面")

    def stop(self) -> None:
        self.running = False
        self._watchdog_stop.set()
        # 等待「建立/切换通道」线程收尾后再停流：否则它可能在我们停流之后
        # 又把流建起来，留下一个没人引用的连接。
        self._join_video_threads(VIDEO_STOP_JOIN)
        self._stop_streams()
        if self._mqtt is not None:
            self._mqtt.stop()
            self._mqtt = None
        if self._watchdog is not None:
            self._watchdog.join(timeout=2.0)
            self._watchdog = None
        self.status.mqtt_online = False
        self.status.camera_online = False

    # ------------------------------------------------------- 视频线程生命周期
    def _spawn_video_thread(self, target, tag: str, *args) -> None:
        """启动一个「建立/切换视频通道」线程，并保留引用以便 stop() 收尾。

        这些线程可能阻塞在 RTSPS 取首帧上（单次最长 20 秒），因此必须能被 stop()
        等待或识别为已退出，否则反复重连会不断累积游离线程。
        """
        with self._stream_lock:
            alive: list[threading.Thread] = []
            for thread in self._video_threads:
                if thread.is_alive():
                    alive.append(thread)
                else:
                    thread.join(timeout=0.1)  # 已退出，回收引用
            self._video_threads = alive
            thread = threading.Thread(
                target=target, args=args, name=f"{tag}-{self.info.ip}", daemon=True
            )
            self._video_threads.append(thread)
            thread.start()

    def _join_video_threads(self, timeout: float) -> None:
        with self._stream_lock:
            threads = [thread for thread in self._video_threads if thread.is_alive()]
            self._video_threads = []
        for thread in threads:
            thread.join(timeout=timeout)

    def _stop_streams(self) -> None:
        with self._stream_lock:
            if self._camera is not None:
                self._camera.stop()
                self._camera.join(timeout=3.0)
                self._camera = None
            if self._rtsp is not None:
                self._rtsp.stop()
                self._rtsp.join(timeout=3.0)
                self._rtsp = None

    def _mqtt_watchdog_tick(self) -> None:
        """遥测自愈：paho 的重连卡死时，整条重建连接。

        ## 为什么需要它

        paho 自己会重连（``loop_start`` + ``reconnect_delay_set``），那足以
        应付打印机主动断开、网络抖动这类情况。但它**也会卡死**：

        * 网络切换过（Wi-Fi 关了再开、Android 息屏）之后，底层 socket 状态坏掉，
          paho 的退避重连一直失败却不再报错；
        * 初始 connect 一直失败时它停在断开态，不会重新走一遍 TLS 探测。

        症状就是「**画面正常·遥测断开**」时不时出现、而且**不再自愈** ——
        用户只能手动点「重连」。这个 tick 就是兜住那种情况的。

        ## 判据

        看 `MqttWorker.connect_count` **有没有涨**，而不是看状态字符串：

        * 一直在涨 -> paho 正常工作，不打扰；
        * 超过 90 秒没涨过 -> 卡死了，重建一条。

        这样不会和 paho 自身的重连打架（它每次连上都会让计数 +1），
        也不会因为打印机偶尔断开就频繁重建。
        """
        worker = self._mqtt
        if worker is None or not self.running:
            return
        now = time.time()
        last_ok = max(worker.last_connected_at, worker.first_attempt_at)
        if not last_ok or (now - last_ok) < MQTT_STUCK_SECONDS:
            return
        if getattr(worker, "connect_count", 0) > self._mqtt_seen_connects:
            # 期间连上过：说明 paho 在正常重连，把基线抬上去继续观察
            self._mqtt_seen_connects = worker.connect_count
            return
        self.warnings.append("遥测连接疑似卡死，正在重建（约每 90 秒检查一次）")
        LOGGER.warning("MQTT 疑似卡死，重建连接：%s", self.info.ip)
        try:
            worker.restart()
        except Exception:  # noqa: BLE001 - 自愈失败不能把看门狗线程带走
            LOGGER.exception("重建 MQTT 连接失败：%s", self.info.ip)

    def _video_watchdog(self) -> None:
        """周期性自检并自愈：

        1. **视频通道**：画面长时间没有新帧时，在两个通道之间切换一次；
        2. **MQTT 遥测**：连接卡死时整条重建（见 :meth:`_mqtt_watchdog_tick`）。

        真实场景里同一个型号的不同固件，可能只有其中一个通道可用
        （例如 H2/X2D 系列只提供 RTSPS，而 A1/P1 只有 6000 端口 JPEG 流）。
        首次探测失败并不代表永远失败，因此这里做周期性重试而不是一次性放弃。
        """
        last_stream = None
        last_count = -1
        stale = 0
        while not self._watchdog_stop.wait(WATCHDOG_INTERVAL):
            if not self.running:
                return

            self._mqtt_watchdog_tick()

            # 情况一：6000 端口拒绝了正确口令（遥测已连上说明口令没错），
            # 说明该机型不支持 6000 通道，直接切回 RTSPS。
            camera = self._camera
            if (
                camera is not None
                and camera.state == CameraStream.STATE_AUTH_ERROR
                and self.status.mqtt_online
                and self.info.model.supports_rtsp
            ):
                self.warnings.append("6000 端口拒绝了正确口令，说明该机型不支持该通道，改用 RTSPS")
                self._switch_channel("rtsp")
                return

            stream = self._camera if self._camera is not None else self._rtsp
            if stream is None:
                continue
            if stream is not last_stream:
                last_stream, last_count, stale = stream, stream.frame_count, 0
                continue
            count = stream.frame_count
            if count == last_count:
                stale += 1
            else:
                stale = 0
            last_count = count
            if stale >= 4:  # 约 60 秒没有新画面
                stale = 0
                if not self.info.access_code:
                    continue
                channel = self.info.model.video_channel
                if channel == "tcp6000":
                    continue  # 该机型只有 6000 一条路，流自身会重连
                if channel == "rtsp":
                    target = "rtsp"  # 该机型只有 RTSPS，绝不能退回 6000
                else:
                    target = "tcp6000" if self.video_channel == "rtsp" else "rtsp"
                self.warnings.append(f"画面长时间无更新，正在切换到 {target} 通道重试")
                self._switch_channel(target)
                return

    def _switch_channel(self, target: str) -> None:
        self._spawn_video_thread(self._start_camera, "video-switch", target)

    def restart(self) -> None:
        """重连：先彻底停干净，再启动。

        原来这里是 ``stop()`` + ``sleep(0.2)`` + ``start()``。但视频建立线程
        可能正卡在 RTSPS 取首帧上（单次最长 20 秒），0.2 秒根本等不到 ——
        于是新会话起来后，上一轮的线程还在后台接着干活（又去连一次打印机），
        表现为「重连后画面偶尔又断一次」。``stop()`` 内部已经会 join 线程，
        这里再显式等一次，确保真的是「停了再起」。
        """
        self.stop()
        self._join_video_threads(VIDEO_RESTART_JOIN)
        self.start()

    # ------------------------------------------------------------------ 内部
    def _emit(self, event: str) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                pass

    def _start_telemetry(self) -> None:
        if not self.info.serial:
            self.warnings.append("缺少序列号，无法订阅状态报文（可在编辑窗口手动填写）")
            return
        self._mqtt = MqttWorker(
            host=self.info.ip,
            serial=self.info.serial,
            access_code=self.info.access_code,
            on_report=self._handle_report,
            on_state=self._handle_mqtt_state,
            name=self.info.display_name(),
        )
        self._mqtt.start()

    def _preferred_channel(self) -> str:
        """按机型能力与用户设置决定首选视频通道。"""
        mode = (self.info.stream_mode or "auto").lower()
        if mode == "tcp6000":
            return "tcp6000"
        if mode == "rtsp":
            return "rtsp"
        channel = self.info.model.video_channel
        if channel == "rtsp":
            from .rtsp import RtspStream  # 延迟导入：OpenCV 为可选依赖

            if RtspStream.available():
                return "rtsp"
            self.warnings.append("未安装 opencv-python，无法使用 RTSPS；该机型不支持 6000 端口画面")
            return "tcp6000"
        if channel == "tcp6000":
            return "tcp6000"
        # 未知机型：先试 RTSPS，失败再退 6000
        from .rtsp import RtspStream

        return "rtsp" if RtspStream.available() else "tcp6000"

    def _start_camera(self, prefer: Optional[str] = None) -> None:
        """启动视频通道；prefer 为空时按机型能力自动选择。

        整个过程用 ``_video_setup_lock`` 串行化：看门狗的通道切换与首帧建立
        若并发执行，会互相把对方的流停掉，表现为「画面刚出来又断」。
        """
        if not self.running:
            return
        with self._video_setup_lock:
            self._start_camera_locked(prefer)

    def _start_camera_locked(self, prefer: Optional[str] = None) -> None:
        """真正建立视频通道（调用方必须已持有 ``_video_setup_lock``）。"""
        if not self.running:
            return
        # 换道前先停掉旧流，否则旧通道会变成没人引用的游离线程继续占用连接。
        # 这里只取 _stream_lock，不会与 _video_setup_lock 形成环。
        self._stop_streams()
        channel = (prefer or self._preferred_channel()).lower()
        rtsp_only = self.info.model.video_channel == "rtsp"

        if channel == "rtsp":
            from .rtsp import RtspStream  # 延迟导入：OpenCV 为可选依赖

            if not RtspStream.available():
                self.warnings.append("未安装 opencv-python，无法使用 RTSPS，改用 6000 端口")
                channel = "tcp6000"
            else:
                # 首次拉流要等 FFmpeg 初始化（打包版首次加载 cv2 可能更慢），给足时间；
                # 只有 RTSPS 一条路的机型多试几次，避免偶发失败就长时间没画面
                attempts = 3 if rtsp_only else 2
                for attempt in range(attempts):
                    if not self.running:
                        return
                    holder: list = []
                    stream = RtspStream(
                        host=self.info.ip,
                        access_code=self.info.access_code,
                        on_state=self._state_callback(holder),
                        name=self.info.display_name(),
                    )
                    holder.append(stream)
                    stream.start()
                    if stream.wait_first_frame(RTSP_ADOPT_TIMEOUT) is not None and self.running:
                        self._rtsp = stream
                        self.video_channel = "rtsp"
                        # 采纳该流之后同步一次状态，否则界面还停留在「未连接」
                        self._handle_stream_state(stream, stream.state, stream.detail)
                        return
                    stream.stop()
                    stream.join(timeout=RTSP_STREAM_JOIN)
                    if not self.running:
                        return
                    if attempt + 1 < attempts:
                        self.warnings.append(
                            f"RTSPS 未取到画面，第 {attempt + 2} 次重试…"
                        )
                        time.sleep(RTSP_RETRY_PAUSE)
                if rtsp_only:
                    # 该机型只有 RTSPS 一条路，绝不能退回 6000（那边一定失败）。
                    # 交由看门狗周期性重试。
                    self.warnings.append(
                        "RTSPS(322) 未取到画面：请在打印机屏幕上开启「局域网实时画面 / LAN Mode Liveview」"
                    )
                    self.video_channel = "rtsp"
                    self._set_state_hint("RTSPS 未取到画面，等待重试")
                    return
                self.warnings.append("RTSPS(322) 未取到画面，已回退到 6000 端口")
                channel = "tcp6000"

        self.video_channel = "tcp6000"
        holder = []
        camera = CameraStream(
            host=self.info.ip,
            access_code=self.info.access_code,
            serial=self.info.serial,
            on_state=self._state_callback(holder),
            name=self.info.display_name(),
        )
        holder.append(camera)
        self._camera = camera
        camera.start()

    def _state_callback(self, holder: list):
        """生成带来源标识的状态回调，便于丢弃已经切换掉的通道的事件。"""

        def callback(state: str, detail: str) -> None:
            source = holder[0] if holder else None
            self._handle_stream_state(source, state, detail)

        return callback

    def _set_state_hint(self, detail: str) -> None:
        """没有活动视频流时，也要给界面一个明确的状态说明。"""
        self.last_camera_state = CameraStream.STATE_RETRYING
        self.last_camera_detail = detail
        self.status.camera_online = False
        self.status.last_error = detail
        self._emit("camera")

    def _handle_stream_state(self, source: object, state: str, detail: str) -> None:
        """只接受当前生效通道的状态，避免旧通道的收尾回调覆盖新通道。"""
        active = {id(self._camera), id(self._rtsp)}
        if source is not None and id(source) not in active:
            return
        self.last_camera_state = state
        self.last_camera_detail = detail
        if (
            state == CameraStream.STATE_AUTH_ERROR
            and self.status.mqtt_online
            and self.info.model.supports_rtsp
        ):
            # 口令没错（遥测都通了），那就不是访问代码的问题
            detail = "6000 端口不支持该机型，正在改用 RTSPS(322) 通道"
            self.last_camera_detail = detail
        self.status.camera_online = state == CameraStream.STATE_STREAMING
        if state == CameraStream.STATE_AUTH_ERROR:
            self.status.last_error = detail
        self._emit("camera")

    def _handle_report(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.status.apply_report(payload)
            info = payload.get("info")
            if isinstance(info, dict):
                modules = info.get("module")
                if isinstance(modules, list):
                    for module in modules:
                        if isinstance(module, dict) and module.get("name") == "ota":
                            version = str(module.get("sw_ver", "") or "")
                            if version and self.info.firmware != version:
                                self.info.firmware = version
        self.status.mqtt_online = True
        self._emit("telemetry")

    def _handle_mqtt_state(self, state: str, detail: str) -> None:
        self.status.mqtt_online = state == MqttWorker.STATE_ONLINE
        self.mqtt_auth_error = state == MqttWorker.STATE_AUTH_ERROR
        if state == MqttWorker.STATE_AUTH_ERROR:
            self.status.last_error = detail
        self._emit("telemetry")

    def _handle_camera_state(self, state: str, detail: str) -> None:
        """兼容旧调用（无来源标识时按当前通道处理）。"""
        self._handle_stream_state(None, state, detail)

    # ------------------------------------------------------------------ 查询
    def latest_frame(self) -> tuple[int, Optional[bytes]]:
        if self._camera is not None:
            return self._camera.latest_frame()
        if self._rtsp is not None:
            return self._rtsp.latest_frame()
        return 0, None

    def set_video_target_size(self, width: int, height: int) -> None:
        """告诉视频通道画面控件的尺寸，便于按需缩放/编码（降低 CPU 占用）。"""
        stream = self._rtsp
        if stream is not None:
            try:
                stream.set_target_size(width, height)
            except Exception:
                pass

    def set_max_fps(self, fps: float) -> None:
        """限制每路画面的最大帧率（0 表示不限制）。"""
        stream = self._rtsp
        if stream is not None:
            try:
                stream.set_max_fps(fps)
            except Exception:
                pass

    # ------------------------------------------------------------------ 能力
    @property
    def capabilities(self) -> "DeviceCapabilities":
        """这台设备「能做什么」（机型固有能力 + **运行时实际上报**）。

        界面与网页一律读这个，**不要**再去读 `info.model.has_chamber_sensor` 这类
        机型属性——那样会把「按能力分支」退化成「按机型分支」，接入第三方设备族时
        每一处都要改。

        运行时观测覆盖机型推断的两处（都为修正实测与规格不符的情况）：

        * ``can_control_light``：设备只要上报了 ``lights_report`` 节点，就说明它**真的有灯**。
          实测教训：A2L 是开放式机型，按规格推断"无舱灯"，但真机上报了
          ``chamber_light`` —— 于是界面把灯按钮藏了，用户没法开关灯。
          **机型规格推不出灯光能力，只有设备上报才算数。**
        * ``nozzle_count``：H2D 这类双喷嘴机型只有在上报第二路温度时才真的是 2。
        """
        base = self.info.model.capabilities
        changes: dict[str, object] = {}
        if self.status.lights:
            # 设备上报了灯（哪怕只有一个节点）→ 允许控制
            changes["can_control_light"] = True
        if self.status.nozzle_temper_2 is not None:
            changes["nozzle_count"] = max(2, int(base.nozzle_count))
        return base.merged(**changes) if changes else base

    # ------------------------------------------------------------------ 控制
    @property
    def can_control(self) -> bool:
        """遥测是否在线（**能否下发**命令的通用前提）。

        注意这里**不再**把"固件要求签名"算进来：签名要求只覆盖 **`print` 段**的命令
        （暂停/停止/速度），而 `system` 段的命令（开关灯）不受影响 —— 实测确认
        A2L 在 `needs_mqtt_signature=True` 的情况下，灯控**确实生效**。
        把两者混在一起会把本来能用的灯控也拦掉。

        具体某条命令是否可用，请查 :meth:`command_blocked`。
        """
        return self._mqtt is not None and self.status.mqtt_online

    @property
    def controls_blocked(self) -> bool:
        """是否有**任何**控制被固件签名要求挡住（用于界面提示）。"""
        return self.status.needs_mqtt_signature is True

    def command_blocked(self, command: str) -> str:
        """某条归一化命令是否被挡住；被挡住时返回原因，可用则返回空字符串。

        只有 **`print` 段**的命令会因签名要求失效 —— 依据是官方 MQTT 签名文档：
        "Firmware rejects unsigned `print` commands when Developer Mode is off"，
        且顶层不带 `print` 的报文（`system` / `info`）不在该机制的覆盖范围内。
        实测印证：同一台 A2L 上 `system/ledctrl` 生效、`print/pause` 与
        `print/print_speed` 被忽略。
        """
        if not self.controls_blocked:
            return ""
        if command in self.SIGNATURE_SENSITIVE_COMMANDS:
            return self.controls_blocked_reason
        return ""

    @property
    def controls_blocked_reason(self) -> str:
        """被挡住时给用户看的说明（要能直接指导用户解决问题）。

        这段文案是**用户唯一的线索**，所以必须把三条可行路径都写出来，
        并说清"登录拓竹账号没用"。实测结论（见 docs/FIELD_NOTES.md）：

        * 第三方客户端（包括本项目）只走**局域网**接口，连云端账号不能下发控制；
        * 从某版固件起，`print` 段的命令要求签名，而签名密钥只给官方
          Bambu Studio / 农场管家一类应用 —— 第三方要能控制，必须由用户在
          打印机上放行（开局域网模式 / 开开发者模式）。
        """
        if not self.controls_blocked:
            return ""
        return (
            "暂停 / 停止 / 速度档位这类命令被打印机固件拒绝了。\n\n"
            "原因是固件要求 MQTT 命令签名，而签名密钥只给官方应用 —— "
            "第三方软件（包括本项目）拿不到，所以这些命令会被设备直接忽略。\n\n"
            "在打印机屏幕上任选一条放行即可（改完重启设备）：\n"
            "  1. 开启「局域网模式」：设置 → 网络 → 局域网模式 / LAN Mode Only\n"
            "  2. 开启「开发者模式」：设置 → 通用 → 开发者模式 / Developer Mode\n"
            "  3. 改用官方「农场管家 / Farm Manager」接管这台设备\n\n"
            "注意：只在切片软件里**登录拓竹账号**是不够的 —— 那只影响云端，"
            "第三方客户端走的是局域网接口，需要上面其中一条。\n\n"
            "开关灯不受影响（灯控走 system 段命令，不在签名要求范围内），"
            "所以状态条上仍会显示开灯/关灯按钮，且可以正常使用。\n\n"
            "若设备报 HMS_0500-0500-0001-0007 即为此原因，"
            "详见 https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/0500_0500_0001_0007"
        )

    def _command(self, section: str, command: str, **fields: Any) -> bool:
        worker = self._mqtt
        if worker is None or not self.status.mqtt_online:
            return False
        # 归一化命令名用于查签名门禁（section 决定它是否受影响）
        normalized = {"print_speed": "speed", "pause": "pause", "resume": "resume",
                      "stop": "stop", "ledctrl": "light"}.get(command, command)
        if self.command_blocked(normalized):
            # 已知会被固件忽略：不要假装成功（返回 True 会让用户以为点了生效）
            LOGGER.info("命令 %s 因固件签名要求被拦下，未下发", command)
            return False
        return worker.publish_command(section, command, **fields)

    def pause_print(self) -> bool:
        """暂停打印。"""
        return self._command("print", "pause", param="")

    def resume_print(self) -> bool:
        """继续打印。"""
        return self._command("print", "resume")

    def stop_print(self) -> bool:
        """停止打印（不可恢复，请谨慎）。"""
        return self._command("print", "stop")

    def set_light(self, on: bool) -> bool:
        """开/关舱灯（chamber_light）。"""
        return self._command(
            "system",
            "ledctrl",
            led_node="chamber_light",
            led_mode="on" if on else "off",
            led_on_time=500,
            led_off_time=500,
            loop_times=0,
            interval_time=0,
        )

    def set_speed(self, level: int) -> bool:
        """打印速度档位：1 静音 / 2 标准 / 3 运动 / 4 狂暴。"""
        return self._command("print", "print_speed", param=str(max(1, min(4, int(level)))))

    def snapshot(self) -> PrinterStatus:
        """返回当前状态快照。

        「画面在线」直接用最近一帧的新鲜度判定，比连接状态字符串更可靠
        （即使状态机短暂抖动，只要画面还在动就应显示为在线）。
        """
        with self._lock:
            stream = self._camera if self._camera is not None else self._rtsp
            if stream is not None:
                self.status.camera_online = stream.last_frame_age < 6.0
            return self.status

    @property
    def camera_fps(self) -> float:
        if self._camera is not None:
            return self._camera.fps
        if self._rtsp is not None:
            return self._rtsp.fps
        return 0.0

    @property
    def video_backend(self) -> str:
        if self._rtsp is not None:
            return "RTSPS"
        if self._camera is not None:
            return "TCP6000"
        return "-"

    def connection_summary(self) -> str:
        status = self.snapshot()
        if status.mqtt_online and status.camera_online:
            return "在线"
        if status.mqtt_online or status.camera_online:
            return "部分在线"
        return "离线"
