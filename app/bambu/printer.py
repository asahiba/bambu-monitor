"""把遥测（MQTT）与视频（6000 端口 / RTSPS）聚合成一台打印机的监控会话。"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from .camera import CameraStream
from .models import PrinterInfo, PrinterStatus
from .mqtt_worker import MqttWorker


class PrinterSession:
    """一台打印机的完整监控会话（遥测 + 视频）。

    本类不依赖 GUI：界面通过 ``snapshot()`` 轮询状态，通过
    ``latest_frame()`` 取最新一帧，因此不会因为高频信号导致队列堆积。
    """

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
        self._join_video_threads(2.0)
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

    def _video_watchdog(self) -> None:
        """画面长时间没有新帧时，自动在两个视频通道之间切换一次。

        真实场景里同一个型号的不同固件，可能只有其中一个通道可用
        （例如 H2/X2D 系列只提供 RTSPS，而 A1/P1 只有 6000 端口 JPEG 流）。
        首次探测失败并不代表永远失败，因此这里做周期性重试而不是一次性放弃。
        """
        last_stream = None
        last_count = -1
        stale = 0
        while not self._watchdog_stop.wait(15.0):
            if not self.running:
                return

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
        self.stop()
        time.sleep(0.2)
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
                    if stream.wait_first_frame(20.0) is not None and self.running:
                        self._rtsp = stream
                        self.video_channel = "rtsp"
                        # 采纳该流之后同步一次状态，否则界面还停留在「未连接」
                        self._handle_stream_state(stream, stream.state, stream.detail)
                        return
                    stream.stop()
                    stream.join(timeout=2.0)
                    if not self.running:
                        return
                    if attempt + 1 < attempts:
                        self.warnings.append(
                            f"RTSPS 未取到画面，第 {attempt + 2} 次重试…"
                        )
                        time.sleep(4.0)
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

    # ------------------------------------------------------------------ 控制
    @property
    def can_control(self) -> bool:
        """遥测在线**且固件允许**下发控制命令。

        注意这里保留了「MQTT 在线」的原始语义（有测试与调用方依赖），
        但因为下面这条实测事实，它现在还要求固件不处于「命令需签名」状态：

        新机型（H2C / H2S / X2D / P2S / **A2L**）的报文里 ``fun`` 字段 bit
        ``0x20000000`` 会置位，表示 **MQTT 命令需要签名校验**。此时若用户没有在
        打印机触屏上开启 Developer Mode，我们下发的命令会被固件**静默忽略**——
        界面看起来就是「点了暂停没反应」，而画面与遥测一切正常。
        与其让按钮看起来能用却无效，不如置灰并说明原因（见 ``controls_blocked``）。
        """
        return self._mqtt is not None and self.status.mqtt_online and not self.controls_blocked

    @property
    def controls_blocked(self) -> bool:
        """控制是否被「固件要求命令签名」挡住（需要用户去开 Developer Mode）。

        只在**明确知道**需要签名时才返回 True：``fun`` 字段缺失或无法解析时是未知，
        此时不能拦（否则老机型会被误伤）。
        """
        return self.status.needs_mqtt_signature is True

    @property
    def controls_blocked_reason(self) -> str:
        """被挡住时给用户看的说明。"""
        if not self.controls_blocked:
            return ""
        return (
            "打印机固件要求 MQTT 命令签名：请在打印机屏幕上开启「开发者模式 / "
            "Developer Mode」后重启设备，否则暂停/停止/开灯等控制指令会被静默忽略"
        )

    def _command(self, section: str, command: str, **fields: Any) -> bool:
        worker = self._mqtt
        if worker is None or not self.status.mqtt_online:
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
