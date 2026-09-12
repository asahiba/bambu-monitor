"""Moonraker / Klipper 设备族适配器。

覆盖所有跑 Moonraker 的机器：Voron、RatRig/RatOS、刷 Klipper 的
Creality / Elegoo / Anycubic，以及 **Snapmaker U1**（出厂即 Klipper + Moonraker）。

## 协议要点（来自官方源码与真机探测，见 docs/FIELD_NOTES.md）

* 遥测：``POST /printer/objects/query``，body ``{"objects": {"print_stats": null, ...}}``
* 状态机：``print_stats.state`` ∈ ``standby/printing/paused/complete/error/cancelled``
* 进度：``virtual_sdcard.progress`` 是 **0.0–1.0 的小数**（拓竹是 0–100 整数，务必换算）
* 层数：``print_stats.info.current_layer`` / ``total_layer``（需要切片器写
  ``SET_PRINT_STATS_INFO``，否则缺失 —— 缺失时保持未知，不要报 0）
* 剩余时间：**没有原生字段**。本适配器不自己估算，而是在 ``raw`` 里保留
  ``print_duration`` 供后续结合 ``/server/files/metadata`` 的 ``estimated_time`` 计算。
  宁可不显示，也不要显示一个错的倒计时。
* 控制：``pause`` / ``resume`` / ``cancel`` 是 HTTP POST，**没有 ``transports`` 限制**；
  但 ⚠️ ``/printer/emergency_stop`` 与整个 ``/printer/control/*`` 系列是
  **WebSocket-only**，HTTP 发不通 —— 因此灯控与速度档位默认不声明支持
  （见 ``capabilities``），而不是发一个必然失败的请求。
* 无 HMS 体系：``faults`` 为空；Klipper 异常通过 ``print_stats.state == "error"``
  或 ``webhooks.state != "ready"`` 体现。
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Optional

from ...core.adapter import PollingDeviceSession
from ...core.capabilities import DeviceCapabilities
from ...core.device import (
    JOB_FAILED,
    JOB_FINISHED,
    JOB_IDLE,
    JOB_OFFLINE,
    JOB_PAUSED,
    JOB_PRINTING,
    JOB_UNKNOWN,
)
from .ws import WebSocketClient, WebSocketError

LOGGER = logging.getLogger("bambu-monitor.moonraker")

#: 一次查询要拿的对象（请求体里的键）。可按需裁剪，但保持精简有利于设备端开销。
QUERY_OBJECTS: tuple[str, ...] = (
    "print_stats",
    "virtual_sdcard",
    "display_status",
    "extruder",
    "heater_bed",
    "webhooks",
    "toolhead",
    "gcode_move",
)

#: `print_stats.state` -> 归一化作业状态
STATE_MAP: dict[str, str] = {
    "standby": JOB_IDLE,
    "printing": JOB_PRINTING,
    "paused": JOB_PAUSED,
    "complete": JOB_FINISHED,
    "cancelled": JOB_IDLE,
    "error": JOB_FAILED,
}

#: `webhooks.state` 为这些值时视为 Klipper 不可用
WEBHOOKS_BAD = frozenset({"shutdown", "error", "startup"})


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_status(
    objects: Mapping[str, Any], *, stale_threshold: float = 30.0
) -> dict[str, Any]:
    """把 Moonraker 的 ``/printer/objects/query`` 结果转成通用字段字典。

    这是本适配器最值得单独测试的部分：**纯函数**，不涉及网络，
    因此可以穷举各种设备差异（缺字段、类型异常、状态机取值）。
    返回的键取自 ``core.MAPPABLE_FIELDS``，直接喂给 ``DeviceStatus.apply_mapped``。

    ⚠️ 真实响应是 ``{"result": {"status": {...}}}`` **两层嵌套**，
    状态对象在 ``result.status`` 里而不是顶层 —— 直接找顶层 ``status`` 会永远解析出空。
    这里同时容忍直接传内层 ``status`` 的写法（便于单元测试）。
    """
    fields: dict[str, Any] = {}
    if not isinstance(objects, Mapping):
        return fields
    container = objects.get("result")
    if isinstance(container, Mapping):
        objects = container
    status = objects.get("status")
    if isinstance(status, Mapping):
        pass
    elif "print_stats" in objects or "virtual_sdcard" in objects:
        # 也接受直接传内层 status 的写法（便于单元测试与复用）
        status = objects
    else:
        return fields

    webhooks = status.get("webhooks")
    klippy_ready = True
    if isinstance(webhooks, Mapping):
        hook_state = str(webhooks.get("state", "") or "").lower()
        klippy_ready = hook_state not in WEBHOOKS_BAD

    stats = status.get("print_stats")
    job_state = JOB_UNKNOWN
    if isinstance(stats, Mapping):
        raw_state = str(stats.get("state", "") or "").lower()
        job_state = STATE_MAP.get(raw_state, JOB_UNKNOWN)
        filename = str(stats.get("filename", "") or "")
        if filename:
            fields["job_name"] = filename

        # 层数：切片器没写 SET_PRINT_STATS_INFO 时 info 为空 —— 保持未知而不是报 0
        info = stats.get("info")
        if isinstance(info, Mapping):
            current = _as_int(info.get("current_layer"))
            total = _as_int(info.get("total_layer"))
            if current is not None:
                fields["layer_current"] = max(0, current)
            if total is not None:
                fields["layer_total"] = max(0, total)

    if not klippy_ready:
        # Klipper 处于 shutdown/error：它连状态查询都未必回应，
        # 此时把设备标为离线比显示一个可疑的读数更诚实
        fields["job_state"] = JOB_OFFLINE
    else:
        fields["job_state"] = job_state

    # 进度：virtual_sdcard.progress 是 0.0–1.0；display_status.progress 是 M73 的 0–1
    for source in ("virtual_sdcard", "display_status"):
        block = status.get(source)
        if isinstance(block, Mapping):
            fraction = _as_float(block.get("progress"))
            if fraction is not None:
                fields["progress_percent"] = max(0, min(100, round(fraction * 100)))
                break

    extruder = status.get("extruder")
    if isinstance(extruder, Mapping):
        temperature = _as_float(extruder.get("temperature"))
        target = _as_float(extruder.get("target"))
        if temperature is not None:
            fields["nozzle_temper"] = temperature
        if target is not None:
            fields["nozzle_target_temper"] = target

    bed = status.get("heater_bed")
    if isinstance(bed, Mapping):
        temperature = _as_float(bed.get("temperature"))
        target = _as_float(bed.get("target"))
        if temperature is not None:
            fields["bed_temper"] = temperature
        if target is not None:
            fields["bed_target_temper"] = target

    return fields

class MoonrakerAdapter(PollingDeviceSession):
    """一台 Moonraker 设备的监控会话。

    :param base_url: 例如 ``http://192.168.1.77`` 或 ``http://192.168.1.77:7125``
    :param api_key: 可选。内网默认免鉴权；非标准网段可能需要（``X-Api-Key``）
    :param camera_url: 摄像头 URL。留空时尝试用 ``/server/webcams/list`` 自动发现；
        两者都拿不到就退化为"只监控无画面"（U1 原厂固件尤其需要手动给
        ``http://<ip>/server/files/camera/monitor.jpg``，因为它没有标准 MJPEG 端点）
    """

    family_label = "Moonraker"
    default_poll_interval = 2.0
    #: 摄像头保活间隔（秒）。U1 的监视会话几分钟就会自动关闭，所以要比它更勤。
    KEEPALIVE_INTERVAL = 5.0

    def __init__(
        self,
        info: Any,
        *,
        base_url: str = "",
        api_key: str = "",
        camera_url: str = "",
        led_name: str = "",
        ws_url: str = "",
        timeout: float = 6.0,
        on_keepalive: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(info, **kwargs)
        host = getattr(info, "ip", "") or ""
        port = getattr(info, "port", 0) or 80
        self.base_url = (base_url or f"http://{host}:{port}").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        #: 可选钩子：每次轮询后调用一次，用于自定义保活逻辑
        self._on_keepalive = on_keepalive
        self._camera_url = camera_url
        self._camera_probed = bool(camera_url)
        self._last_keepalive = 0.0
        #: 舱灯的 Moonraker 对象名（例如 U1 是 ``cavity_led``）。
        #: 留空表示不声明灯控能力 —— 不猜名字，猜错会发一个必然失败的请求。
        self.led_name = led_name
        #: WebSocket 地址。留空表示「与 HTTP 同主机同端口」（真实 Moonraker 就是
        #: 同端口先握手再升级）；测试里假服务器用独立端口，所以可以显式指定。
        self.ws_url = ws_url
        self._ws: Optional[WebSocketClient] = None
        self._ws_lock = threading.Lock()

        self.capabilities = DeviceCapabilities(
            # 视频形态由运行时探测决定：有 URL 就是快照轮询，没有就退化为无画面
            video_channel="http_snapshot" if camera_url else "none",
            has_camera=bool(camera_url),
            # Klipper 机器普遍没有腔温探头；有的话由设备族扩展声明
            has_chamber_sensor=False,
            # U1 是四喷嘴工具头机型，但"几个喷嘴"取决于实际配置，默认 1 更保守
            nozzle_count=1,
            # Klipper 不上报 WiFi 信号强度
            has_wifi_signal=False,
            supports_ams=False,
            supports_external_spool=True,
            can_pause=True,
            can_stop=True,
            # ⚠️ 灯控与速度档位走 WebSocket-only 的 /printer/control/*（HTTP 发不通）。
            # 灯需要知道对象名才声明支持（猜错会必然失败）；速度不需要额外参数，
            # 有 WS 通道就能用。
            can_control_light=bool(led_name),
            can_set_speed=True,
            # Klipper 没有 HMS 体系
            has_hms=False,
        )
        self.video_backend = "快照" if camera_url else "-"

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """启动前先探测一次摄像头。

        基类只在 ``capabilities.has_camera`` 为真时才轮询画面，而摄像头 URL 是
        **运行时才知道**的（要么用户填、要么从 ``/server/webcams/list`` 发现）。
        所以这里先探一次，否则会出现"明明有摄像头却永远不取画面"。
        探测失败不影响遥测：退化成"只监控、无画面"。
        """
        if not self._camera_url:
            self._discover_camera()
        super().start()

    # ------------------------------------------------------------------ 传输
    def _request(
        self, path: str, payload: Optional[dict] = None, *, method: str = "GET"
    ) -> Any:
        """发一次 HTTP 请求并返回解析后的 JSON；失败抛异常（由基类记成失败）。"""
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.api_key:
            request.add_header("X-Api-Key", self.api_key)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read()
        if not body:
            return {}
        return json.loads(body.decode("utf-8", errors="replace"))

    def _fetch_status(self) -> Mapping[str, Any]:
        payload = {"objects": {name: None for name in QUERY_OBJECTS}}
        raw = self._request("/printer/objects/query", payload, method="POST")
        self.status.raw.update(raw if isinstance(raw, dict) else {})
        self._maybe_keepalive()
        return parse_status(raw)

    def _fetch_frame(self) -> Optional[bytes]:
        if not self._camera_url:
            self._discover_camera()
            if not self._camera_url:
                return None
        try:
            request = urllib.request.Request(self._camera_url)
            if self.api_key:
                request.add_header("X-Api-Key", self.api_key)
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # 画面拿不到不算设备离线，只记通道说明
            self.last_camera_detail = f"摄像头不可用：{exc}"
            return None
        if not body.startswith(b"\xff\xd8"):
            self.last_camera_detail = "摄像头返回的不是 JPEG（本程序只支持 JPEG 画面）"
            return None
        return body

    def _discover_camera(self) -> None:
        """用官方端点 ``/server/webcams/list`` 自动发现摄像头 URL。

        只探测一次，失败就永久退化为"无画面"，避免每轮都白试。
        """
        if self._camera_probed:
            return
        self._camera_probed = True
        try:
            data = self._request("/server/webcams/list")
        except Exception as exc:  # noqa: BLE001 - 探测失败很常见，不值得报错
            LOGGER.debug("webcams/list 探测失败：%s", exc)
            return
        webcams = data.get("result", {}).get("webcams") if isinstance(data, dict) else None
        if not isinstance(webcams, list):
            return
        for webcam in webcams:
            if not isinstance(webcam, dict):
                continue
            # 优先用快照（单帧 JPEG），因为本程序按帧取用，不解析 MJPEG 流
            url = str(webcam.get("snapshot_url") or webcam.get("stream_url") or "")
            if url:
                self._camera_url = self._absolute(url)
                self.capabilities = self.capabilities.merged(
                    has_camera=True, video_channel="http_snapshot"
                )
                self.video_backend = "快照"
                LOGGER.info("自动发现摄像头：%s", self._camera_url)
                return

    def _absolute(self, url: str) -> str:
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return f"{self.base_url}{url if url.startswith('/') else '/' + url}"

    def _maybe_keepalive(self) -> None:
        """周期性给摄像头续命，否则画面会静止。

        **Snapmaker U1 真机实测**：``/server/files/camera/monitor.jpg`` 在没人调用
        ``camera.start_monitor`` 时会一直是陈旧旧帧。而这个方法**只能走 WebSocket**
        （官方源码里 camera 端点带 ``~HTTP``），所以保活与灯控/速度共用同一条 WS 连接。

        自定义钩子 ``on_keepalive`` 优先：它可以返回一个新的摄像头 URL
        （例如重新签发 token 后），本方法会采纳它。
        """
        now = time.time()
        if now - self._last_keepalive < self.KEEPALIVE_INTERVAL:
            return
        self._last_keepalive = now

        if self._on_keepalive is not None:
            try:
                updated = self._on_keepalive(self)
            except Exception as exc:  # noqa: BLE001 - 保活失败不应影响遥测
                LOGGER.debug("摄像头保活钩子失败：%s", exc)
            else:
                if isinstance(updated, str) and updated:
                    self._camera_url = self._absolute(updated)
                    self._adopt_camera()
            return

        if not self._camera_url:
            return
        # 默认保活：对 U1 这类设备是"能否看到画面"的前提
        self._ws_call("camera.start_monitor", {"domain": "lan", "interval": 0})

    def _adopt_camera(self) -> None:
        self.capabilities = self.capabilities.merged(
            has_camera=True, video_channel="http_snapshot"
        )
        self.video_backend = "快照"

    def _send_command(self, command: str, **params: Any) -> bool:
        """下发控制命令。

        * ``pause`` / ``resume`` / ``stop`` → HTTP POST（官方这三个端点没有
          ``transports`` 限制，HTTP 可用）。
        * ``light`` / ``speed`` → **WebSocket**（``/printer/control/*`` 是 WS-only，
          HTTP 发不通）。WS 不可用时返回 False，而不是发一个必然失败的 HTTP 请求。
        """
        http_endpoint = {
            "pause": "/printer/print/pause",
            "resume": "/printer/print/resume",
            "stop": "/printer/print/cancel",
        }.get(command)
        if http_endpoint is not None:
            try:
                self._request(http_endpoint, {}, method="POST")
                return True
            except (urllib.error.URLError, OSError, ValueError) as exc:
                LOGGER.warning("Moonraker 下发 %s 失败：%s", command, exc)
                return False

        if command == "light":
            if not self.led_name:
                return False
            # ⚠️ 参数名是 `name`（Moonraker 包装层再映射成 Klipper 的 mux key `led`），
            # 传 `led` 会因缺必填项报错；且该层取整数 → 只能 0/1 开关，不能调光。
            return self._ws_call(
                "printer.control.led", {"name": self.led_name, "white": 1 if params.get("on") else 0}
            )

        if command == "speed":
            # ⚠️ 参数名是 `percentage`；且官方限定"仅打印中可用"
            level = int(params.get("level", 2) or 2)
            percentage = {1: 50, 2: 100, 3: 133, 4: 166}.get(max(1, min(4, level)), 100)
            return self._ws_call("printer.control.print_speed", {"percentage": percentage})

        LOGGER.info("Moonraker 暂不支持指令 %s", command)
        return False

    # ------------------------------------------------------------------ WebSocket
    def _ensure_ws(self) -> Optional[WebSocketClient]:
        """取（必要时建立）WebSocket 连接。失败返回 None，由调用方降级。"""
        with self._ws_lock:
            if self._ws is not None and self._ws.connected:
                return self._ws
            if self._ws is not None:
                self._ws.close()
                self._ws = None
            if self.ws_url:
                url = self.ws_url
            else:
                url = self.base_url.replace("https://", "wss://").replace("http://", "ws://")
                url = f"{url}/websocket"
            client = WebSocketClient(url, api_key=self.api_key, timeout=self.timeout)
            try:
                client.connect()
            except (WebSocketError, OSError, ValueError) as exc:
                LOGGER.debug("Moonraker WebSocket 连接失败：%s", exc)
                return None
            self._ws = client
            return client

    def _ws_call(self, method: str, params: Optional[dict] = None) -> bool:
        """发一条 WS JSON-RPC 调用；失败返回 False（不抛异常）。"""
        client = self._ensure_ws()
        if client is None:
            return False
        try:
            client.call(method, params)
            return True
        except (WebSocketError, OSError, ValueError) as exc:
            LOGGER.warning("Moonraker WS 调用 %s 失败：%s", method, exc)
            with self._ws_lock:
                if self._ws is client:
                    client.close()
                    self._ws = None
            return False

    def emergency_stop(self) -> bool:
        """急停。**只能走 WebSocket**（官方源码里该端点带 ``~HTTP``）。

        没有可靠的 HTTP 替代：``M112`` 是否能经 ``/printer/gcode/script`` 放行未经验证，
        因此这里不做兜底 —— 急停不成功时必须让用户知道，而不是假报成功。
        """
        return self._ws_call("printer.emergency_stop")

    def stop_ws(self) -> None:
        with self._ws_lock:
            if self._ws is not None:
                self._ws.close()
                self._ws = None

    def stop(self) -> None:
        self.stop_ws()
        super().stop()


def probe_http(host: str, port: int, timeout: float = 2.0) -> bool:
    """探测某个 host:port 是否是 Moonraker（用于发现与手动添加时的快速判断）。

    只做一次 TCP 连接尝试，不发业务请求 —— 对未知设备保持最小侵入。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
