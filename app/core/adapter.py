"""轮询型设备适配器基类。

## 它解决什么问题

第三方设备族（Moonraker/Klipper、OctoPrint、PrusaLink…）的共同形态是
**「定时向设备要一次状态、偶尔要一帧画面」**，而拓竹是推送式（MQTT 主动上报）。
把「轮询该怎么写」在每个适配器里重写一遍，就会重复踩同样四个坑：

1. **线程回收**：轮询线程必须在 `stop()` 里可靠退出，否则关掉一路画面后进程里
   还留着游离线程（本项目历史上就踩过，见 `docs/KNOWN_ISSUES.md`）；
2. **限流**：状态轮询（2 秒级）与画面轮询（受帧率上限控制）节奏不同，
   且画面尺寸变化要及时生效；
3. **错误抑制**：网络抖动是常态。单次请求失败不能打断整个监控，
   也不能把异常抛进 Qt 事件循环——要记成告警、退避后继续；
4. **增量合并**：状态必须走 `DeviceStatus.apply_mapped()` 的增量语义，
   不能"整体覆盖"（否则一次只含部分字段的响应用会把其它读数清零）。

这个基类把上述四件事一次做对，子类只需要实现三个传输相关的小方法。

## 子类要做什么

```python
class MoonrakerAdapter(PollingDeviceSession):
    family_label = "Moonraker"

    def __init__(self, info, *, base_url: str, api_key: str = "", **kwargs):
        self._base_url, self._api_key = base_url, api_key
        super().__init__(info, **kwargs)
        self.capabilities = DeviceCapabilities(...)   # 覆盖基类默认值

    def _fetch_status(self) -> Mapping[str, Any]:
        # 取一次状态，返回**通用字段字典**（键取自 core.MAPPABLE_FIELDS）
        ...

    def _fetch_frame(self) -> bytes | None:
        # 取一帧 JPEG（没有摄像头就 return None）
        ...

    def _send_command(self, command: str, **params) -> bool:
        # 下发控制命令；失败返回 False（不要抛异常）
        ...
```

## 帧格式约定

`_fetch_frame()` **必须返回 JPEG 字节**：界面侧的 `ui/frame_decoder.py` 用
`QImageReader` 解码，网页侧的转码与多路复用也都按 JPEG 处理。
若某设备的摄像头只给 PNG，请在适配器里转换，不要指望上层兼容。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Mapping, Optional

from .capabilities import DeviceCapabilities
from .device import DeviceStatus

LOGGER = logging.getLogger("bambu-monitor.adapter")


class PollingDeviceSession:
    """轮询型会话的基类。实现 `DeviceSession` 协议（见 `app.core.__init__`）。

    子类负责：声明 `capabilities`、实现 `_fetch_status` / `_fetch_frame` /
    `_send_command`。其余（线程、限流、错误处理、增量合并）由基类统一保证。
    """

    #: 展示用的设备族名（界面「机型」一栏与诊断信息会用到）
    family_label = "设备"
    #: 没有配置时的默认轮询间隔（秒）
    default_poll_interval = 2.0
    #: 连续失败多少次后延长轮询间隔（避免设备离线时高频空转）
    backoff_after_failures = 3
    #: 退避后的最长轮询间隔（秒）
    max_poll_interval = 15.0

    def __init__(
        self,
        info: Any,
        *,
        on_event: Optional[Any] = None,
        poll_interval: Optional[float] = None,
        max_fps: float = 4.0,
    ) -> None:
        self.info = info
        self.status = DeviceStatus()
        self.capabilities = DeviceCapabilities()
        self.warnings: list[str] = []
        self.running = False
        #: 轮询型设备的「过期」阈值应比推送式更宽松一点
        self.status.stale_seconds = max(10.0, (poll_interval or self.default_poll_interval) * 5)

        self._on_event = on_event
        self._poll_interval = max(0.5, float(poll_interval or self.default_poll_interval))
        self._max_fps = max(0.0, float(max_fps))
        self._target_size: tuple[int, int] = (640, 360)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        #: 最近一次 stop() 停掉的线程；restart() 用它确认旧线程真的退出了
        self._retired_thread: Optional[threading.Thread] = None
        self._latest_frame: Optional[bytes] = None
        self._frame_seq = 0
        self._frame_count = 0
        self._started_at = 0.0
        self._failures = 0
        self.last_camera_state = "stopped"
        self.last_camera_detail = ""
        self.mqtt_auth_error = False
        #: 当前视频通道的展示名（界面直接显示，例如 "MJPG" / "快照" / "-"）
        self.video_backend = "-"

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.warnings.clear()
        self._stop_event.clear()
        self._started_at = time.time()
        self._thread = threading.Thread(
            target=self._poll_loop, name=f"poll-{self.info.ip}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        self._stop_event.set()
        thread, self._thread = self._thread, None
        self._retired_thread = thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self.status.online = False
        self.status.camera_online = False
        self.status.mqtt_online = False

    def restart(self) -> None:
        """整条重建：等上一轮轮询线程真的退出，再启动新一轮。

        原来是 ``stop()`` + ``time.sleep(0.2)`` + ``start()``。轮询线程单次请求
        最长可以阻塞到超时（HTTP 读超时通常是秒级），0.2 秒等不到它退出，
        于是新旧两轮轮询会并行一段时间——两边的 ``_failures``/帧序号互相覆盖，
        表现为「点了重连之后状态反而更乱」。
        """
        self.stop()
        retired = self._retired_thread
        if retired is not None and retired.is_alive():
            retired.join(timeout=2.0)
            if retired.is_alive():
                LOGGER.warning("上一轮轮询线程未在 2 秒内退出，仍启动新一轮：%s", self.info.ip)
        self.start()

    # ------------------------------------------------------------------ 轮询循环
    def _poll_loop(self) -> None:
        next_status = 0.0
        next_frame = 0.0
        while not self._stop_event.is_set():
            now = time.time()
            if now >= next_status:
                self._poll_status()
                interval = self._poll_interval
                if self._failures >= self.backoff_after_failures:
                    # 连续失败：逐步退避，最长 max_poll_interval，避免离线设备被高频轮询
                    interval = min(self.max_poll_interval, interval * (1 + self._failures))
                next_status = time.time() + interval
            if self.capabilities.has_camera and self._max_fps > 0 and now >= next_frame:
                self._poll_frame()
                next_frame = time.time() + (1.0 / self._max_fps)
            elif not self.capabilities.has_camera:
                next_frame = time.time() + 60.0  # 无摄像头：不必频繁判断
            self._stop_event.wait(0.05)

    def _poll_status(self) -> None:
        try:
            fields = self._fetch_status()
        except Exception as exc:  # noqa: BLE001 - 网络抖动是常态，不能打断监控
            self._note_failure(f"状态获取失败：{exc}")
            return
        self._failures = 0
        if not fields:
            return
        changed = self.status.apply_mapped(fields)
        if changed:
            self.last_camera_detail = ""  # 状态恢复了就把上一次的错误说明清掉
            self._emit("telemetry")

    def _poll_frame(self) -> None:
        try:
            frame = self._fetch_frame()
        except Exception as exc:  # noqa: BLE001
            self._set_camera_state(False, f"画面获取失败：{exc}")
            return
        if not frame:
            self._set_camera_state(False, self.last_camera_detail or "等待画面…")
            return
        with self._lock:
            self._latest_frame = frame
            self._frame_seq += 1
            self._frame_count += 1
        self.status.camera_online = True
        self._set_camera_state(True, "画面正常")

    def _note_failure(self, detail: str) -> None:
        self._failures += 1
        self.status.online = False
        self.status.mqtt_online = False
        self.status.last_error = detail
        if self._failures == 1:
            # 只在第一次失败时记告警，避免离线设备把告警列表刷满
            self.warnings.append(detail)
            self._emit("telemetry")
        LOGGER.debug("设备 %s %s（第 %d 次）", self.info.ip, detail, self._failures)

    def _set_camera_state(self, online: bool, detail: str) -> None:
        if self.status.camera_online == online and self.last_camera_detail == detail:
            return
        self.status.camera_online = online
        self.last_camera_state = "streaming" if online else "retrying"
        self.last_camera_detail = detail
        self._emit("camera")

    def _emit(self, event: str) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001 - 回调是界面层的事，不能影响轮询
            LOGGER.debug("事件回调失败", exc_info=True)

    # ------------------------------------------------------------------ 查询
    def snapshot(self) -> DeviceStatus:
        return self.status

    def latest_frame(self) -> tuple[int, Optional[bytes]]:
        with self._lock:
            return self._frame_seq, self._latest_frame

    @property
    def camera_fps(self) -> float:
        elapsed = time.time() - self._started_at if self._started_at else 0.0
        if elapsed <= 0:
            return 0.0
        return self._frame_count / elapsed

    @property
    def can_control(self) -> bool:
        return self.status.online

    @property
    def controls_blocked_reason(self) -> str:
        """默认没有额外门槛（轮询型设备族在自己的协议里鉴权，不走 MQTT 签名）。"""
        return ""

    @property
    def controls_blocked_short(self) -> str:
        """一句话版的挡住原因（协议要求与 :attr:`controls_blocked_reason` 同时提供）。

        它属于 ``DeviceSession`` 契约的一部分：网页端状态条与桌面按钮提示都用它，
        缺了就会在 ``runtime_checkable`` 的协议校验里失败（`tests/test_contracts.py`）。
        """
        return ""

    @property
    def video_unavailable_reason(self) -> str:
        """这台设备在本机**根本出不了画面**时的说明（否则空串）。

        轮询型设备族（Moonraker 等）只要设备有摄像头就能出画面，
        所以默认返回空串；拓竹那边只有「机型仅支持 RTSPS 但本机没有 OpenCV」
        这一种情形（典型是安卓版），见 `app/bambu/printer.py`。
        """
        return ""

    def connection_summary(self) -> str:
        if self.status.online and self.status.camera_online:
            return "在线"
        if self.status.online or self.status.camera_online:
            return "部分在线"
        return "离线"

    # ------------------------------------------------------------------ 控制
    def pause_print(self) -> bool:
        return self._dispatch("pause")

    def resume_print(self) -> bool:
        return self._dispatch("resume")

    def stop_print(self) -> bool:
        return self._dispatch("stop")

    def set_light(self, on: bool) -> bool:
        return self._dispatch("light", on=bool(on))

    def set_speed(self, level: int) -> bool:
        return self._dispatch("speed", level=int(level))

    def _dispatch(self, command: str, **params: Any) -> bool:
        """按能力门禁后下发命令；不支持或失败都返回 False（**不抛异常**）。

        界面与网页对「不支持此指令」与「发送失败」都只看到 False，
        因此子类不需要区分——但应当在自己的日志里写清原因。
        """
        if not self.supports_command(command):
            LOGGER.debug("%s 不支持指令 %s", self.family_label, command)
            return False
        if not self.can_control:
            return False
        try:
            return bool(self._send_command(command, **params))
        except Exception:  # noqa: BLE001 - 控制失败绝不能让界面崩
            LOGGER.warning("%s 下发 %s 失败", self.family_label, command, exc_info=True)
            return False

    def supports_command(self, command: str) -> bool:
        """该设备是否支持这条归一化指令。

        映射关系（`DeviceCapabilities` 的字段名 → 指令名）：
        ``can_pause`` → pause/resume、``can_stop`` → stop、
        ``can_control_light`` → light、``can_set_speed`` → speed。
        """
        caps = self.capabilities
        return {
            "pause": caps.can_pause,
            "resume": caps.can_pause,
            "stop": caps.can_stop,
            "light": caps.can_control_light,
            "speed": caps.can_set_speed,
        }.get(command, False)

    # ------------------------------------------------------------------ 视频参数
    def set_max_fps(self, fps: float) -> None:
        self._max_fps = max(0.0, float(fps))

    def set_video_target_size(self, width: int, height: int) -> None:
        """记录期望的画面尺寸。

        轮询型设备族可以在拉流/转码时用它减少开销；不做也无害（基类只是记录）。
        """
        self._target_size = (max(64, int(width)), max(36, int(height)))

    @property
    def target_size(self) -> tuple[int, int]:
        return self._target_size

    # ------------------------------------------------------------------ 子类实现
    def _fetch_status(self) -> Mapping[str, Any]:
        """取一次状态，返回**通用字段字典**（键取自 `core.MAPPABLE_FIELDS`）。

        返回空字典表示"这次没拿到新数据"（不算失败）。抛异常会被基类记成
        一次失败并退避重试。
        """
        raise NotImplementedError

    def _fetch_frame(self) -> Optional[bytes]:
        """取一帧 **JPEG** 画面；没有摄像头或这次没取到就返回 None。"""
        return None

    def _send_command(self, command: str, **params: Any) -> bool:
        """下发一条归一化控制指令。失败返回 False。"""
        raise NotImplementedError
