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

#: 固定查询的对象。可按需裁剪，但保持精简有利于设备端开销。
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

#: **扩展**对象的对象类型前缀：名字由设备配置决定（例如 ``heater_fan fan0``、
#: ``temperature_sensor EBBCan``、``filament_switch_sensor 断料监测``），
#: 所以启动时按前缀从 ``/printer/objects/list`` 里挑，见 `MoonrakerAdapter._wanted_objects`。
#: 这些读数不会进通用状态模型，而是经 :func:`parse_details` 交给界面显示。
DETAIL_OBJECT_PREFIXES: tuple[str, ...] = (
    "fan",
    "heater_fan",
    "temperature_sensor",
    "filament_switch_sensor",
    "filament_motion_sensor",
    "output_pin",
    "led",
    "neopixel",
    "system_stats",
    "mcu",
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

#: 副画面（非主画面）的抓帧超时上限与连续失败上限。
#: 副画面坏掉时既要尽快放弃，又不能永久放弃（摄像头可能只是还没启动）。
EXTRA_CAMERA_TIMEOUT = 3.0
EXTRA_CAMERA_MAX_FAILS = 3


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

    # 剩余时间：Klipper 没有原生字段，用「已打印时长 ÷ 进度」外推。
    # 进度太小（刚开始）或没有时长时**不猜** —— 显示"--"比显示一个错的倒计时强。
    if isinstance(stats, Mapping):
        printed = _as_float(stats.get("print_duration"))
        percent = fields.get("progress_percent")
        if printed and printed > 0 and isinstance(percent, int) and percent >= 5:
            total_estimate = printed / (percent / 100.0)
            remaining = int(max(0.0, total_estimate - printed) / 60.0)
            if remaining > 0:
                fields["remaining_minutes"] = remaining

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

def _duration_text(seconds: Optional[float]) -> str:
    """秒 -> 「1小时23分」/「45分」/「30秒」。"""
    if not seconds or seconds <= 0:
        return ""
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}小时{minutes:02d}分"
    if minutes:
        return f"{minutes}分{secs:02d}秒"
    return f"{secs}秒"


def _bool_text(value: Any, *, yes: str = "已触发", no: str = "正常") -> str:
    return yes if value else no


def parse_details(objects: Mapping[str, Any]) -> list[dict[str, str]]:
    """把「能看但不参与归一化」的读数整理成界面可以直接显示的条目。

    为什么单独一条：进度/温度这类会进 `DeviceStatus` 的字段是**有限的通用字段**，
    而 Klipper 能给的远不止这些 —— 风扇转速、断料/堵料传感器、工具头板温度、
    主机负载、MCU 版本、耗材用量、屏幕提示……这些各家的名字都不一样，
    塞进通用状态模型会把模型撑爆。

    所以这里返回**展示用的键值对**（``[{"label": "风扇", "value": "60%"}, …]``），
    由界面按族无关的方式直接列出来。缺哪个字段就少一行，不会报错。

    实测（Voron 2.4）：这里有十几项可看，见 `docs/FIELD_NOTES.md` §2.2。
    """
    if not isinstance(objects, Mapping):
        return []
    container = objects.get("result")
    if isinstance(container, Mapping):
        objects = container
    status = objects.get("status")
    if not isinstance(status, Mapping):
        status = objects if "print_stats" in objects else {}
    rows: list[dict[str, str]] = []

    def add(label: str, value: str) -> None:
        if value:
            rows.append({"label": label, "value": value})

    stats = status.get("print_stats")
    if isinstance(stats, Mapping):
        add("已打印", _duration_text(_as_float(stats.get("print_duration"))))
        add("累计时长", _duration_text(_as_float(stats.get("total_duration"))))
        filament = _as_float(stats.get("filament_used"))
        if filament:
            add("耗材用量", f"{filament / 1000:.2f} m" if filament > 1000 else f"{filament:.0f} mm")
        add("提示信息", str(stats.get("message") or ""))

    sdcard = status.get("virtual_sdcard")
    if isinstance(sdcard, Mapping):
        position = _as_float(sdcard.get("file_position"))
        size = _as_float(sdcard.get("file_size"))
        if position and size:
            add("文件位置", f"{position / 1048576:.1f} / {size / 1048576:.1f} MB")

    display = status.get("display_status")
    if isinstance(display, Mapping):
        add("屏幕提示", str(display.get("message") or ""))

    extruder = status.get("extruder")
    if isinstance(extruder, Mapping):
        power = _as_float(extruder.get("power"))
        if power is not None:
            add("喷嘴加热", f"{power * 100:.0f}%")
        if extruder.get("can_extrude") is False:
            add("挤丝", "当前不可挤丝（温度不足或未归零）")
        pressure = _as_float(extruder.get("pressure_advance"))
        if pressure is not None:
            add("压力提前", f"{pressure:g}")

    bed = status.get("heater_bed")
    if isinstance(bed, Mapping):
        power = _as_float(bed.get("power"))
        if power is not None:
            add("热床加热", f"{power * 100:.0f}%")

    toolhead = status.get("toolhead")
    if isinstance(toolhead, Mapping):
        position = toolhead.get("position")
        if isinstance(position, (list, tuple)) and len(position) >= 3:
            add("当前位置", f"X {position[0]:.1f}  Y {position[1]:.1f}  Z {position[2]:.1f}")
        homed = str(toolhead.get("homed_axes") or "").upper()
        if homed:
            missing = [axis for axis in "XYZ" if axis.lower() not in homed.lower()]
            add("归零状态", "XYZ 已归零" if not missing else f"未归零：{'/'.join(missing)}")
        stalls = _as_int(toolhead.get("stalls"))
        if stalls:
            add("丢步", f"{stalls} 次")

    move = status.get("gcode_move")
    if isinstance(move, Mapping):
        parts = []
        speed = _as_float(move.get("speed_factor"))
        if speed is not None:
            parts.append(f"速度 {speed * 100:.0f}%")
        flow = _as_float(move.get("extrude_factor"))
        if flow is not None:
            parts.append(f"流量 {flow * 100:.0f}%")
        add("倍率", " · ".join(parts))

    # 风扇：`fan` 与 `heater_fan <名字>` 都是对象，名字由设备决定
    for name, value in status.items():
        if not isinstance(value, Mapping):
            continue
        if name == "fan" or name.startswith("heater_fan"):
            speed = _as_float(value.get("speed"))
            if speed is not None:
                text = f"{speed * 100:.0f}%"
                rpm = _as_float(value.get("rpm"))
                if rpm:
                    text += f"（{rpm:.0f} rpm）"
                add(f"风扇 {name.replace('heater_fan ', '')}", text)
        elif name.startswith("temperature_sensor"):
            temperature = _as_float(value.get("temperature"))
            if temperature is not None:
                add(f"温度 {name.replace('temperature_sensor ', '')}", f"{temperature:.1f}℃")
        elif name.startswith("filament_switch_sensor"):
            # ⚠️ 语义踩过坑：断料开关的 `filament_detected: true` 表示**有料**
            # （真机实测：正在打印时它是 true）。第一版把两个分支写反了，
            # 于是打印机好好地在打，界面上却写「无料」。
            add(
                f"断料检测 {name.replace('filament_switch_sensor ', '')}",
                "有料" if value.get("filament_detected") else "无料（已触发）",
            )
        elif name.startswith("filament_motion_sensor"):
            # 走料检测给的是**瞬时**状态：挤出间隙、回抽、空驶时读到 false 是正常的。
            # 所以这里只如实转述，**不下"堵料"结论** —— 真机上正是这一条在
            # 正常打印时显示成「可能堵料」，属于自己造出来的假警报。
            add(
                f"走料检测 {name.replace('filament_motion_sensor ', '')}",
                "检测到走料" if value.get("filament_detected") else "未检测到（瞬时值）",
            )
        elif name.startswith("output_pin") or name.startswith("led") or name.startswith("neopixel"):
            add(f"输出 {name}", str(value.get("value", "")))

    system = status.get("system_stats")
    if isinstance(system, Mapping):
        parts = []
        cpu = _as_float(system.get("sysload"))
        if cpu is not None:
            parts.append(f"负载 {cpu:.2f}")
        mem_avail = _as_float(system.get("memavail"))
        mem_total = _as_float(system.get("memtotal"))
        if mem_avail and mem_total:
            parts.append(f"内存 {(mem_total - mem_avail) / mem_total * 100:.0f}%")
        add("主机", " · ".join(parts))

    mcu = status.get("mcu")
    if isinstance(mcu, Mapping):
        version = str(mcu.get("mcu_version") or "")
        if version:
            # 真机给的是 "v0.13.0-770-gce7002bed" 这种长串，界面只要版本号那一段
            add("MCU 固件", version.split("-", 1)[0])

    return rows


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
        #: 用户手填的摄像头地址（留空则自动发现）。见 ``self._cameras``。
        #: 单独留一份原始值：刷新摄像头时要用它把主画面复位。
        self._user_camera_url = camera_url
        self._camera_url = camera_url
        self._camera_probed = bool(camera_url)
        #: 这台机器的**全部**摄像头（真机实测：Voron 上 crowsnest 可能配了多路）。
        #: 每项形如 ``{"index","name","location","url","stream_url","base","fails","detail"}``。
        #: ``index 0`` 是主画面（基类那条 ``latest_frame()`` 通路用的就是它）。
        self._cameras: list[dict[str, Any]] = []
        #: 非主画面的最近一帧：``{index: (seq, jpeg)}``
        self._extra_frames: dict[int, tuple[int, bytes]] = {}
        self._extra_seq = 0
        #: 扩展对象（风扇 / 传感器 / 主机状态…）的清单与最近一次读到的读数。
        self._extra_objects: list[str] = []
        self._extra_objects_probed = False
        self._details: list[dict[str, str]] = []
        #: 相对 URL 的基准地址（**探测出来的**）：Moonraker 的 webcams.list 给的是
        #: 相对路径（``/webcam/?action=snapshot``），它由 crowsnest / 前端挂在**主机
        #: 80 端口**上，Moonraker 自己的端口上并没有这个路径（实测 404）。
        #: 留空表示还没探出来，此时按候选顺序试。
        self._camera_base = ""
        #: 设备是否认识摄像头保活方法（``None`` = 还不知道）。
        #: 普通 Moonraker 没有它（实测返回 -32601），U1 这类设备必须有 —— 见 `_maybe_keepalive`。
        self._keepalive_supported: Optional[bool] = None
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
        self._discover_cameras()
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
        payload = {"objects": {name: None for name in self._wanted_objects()}}
        raw = self._request("/printer/objects/query", payload, method="POST")
        self.status.raw.update(raw if isinstance(raw, dict) else {})
        self._details = parse_details(raw)
        self._maybe_keepalive()
        return parse_status(raw)

    def _wanted_objects(self) -> list[str]:
        """要查询的对象：固定那几个 + **这台设备实际上有的**扩展对象。

        扩展对象的名字由设备配置决定（``heater_fan fan0``、``temperature_sensor EBBCan``、
        ``filament_switch_sensor 断料监测``…），不可能写死在代码里；
        所以启动时读一次 ``/printer/objects/list``，按前缀挑出我们认得的那些。
        查询一个设备没有的对象会被 Klipper 忽略（不报错），但白白增加报文体积，
        所以这里只查确认存在的。
        """
        if not self._extra_objects and not self._extra_objects_probed:
            self._extra_objects_probed = True
            try:
                data = self._request("/printer/objects/list")
                names = (data.get("result") or {}).get("objects") if isinstance(data, dict) else None
                if isinstance(names, list):
                    self._extra_objects = [
                        str(name)
                        for name in names
                        if isinstance(name, str)
                        and (
                            name == "fan"
                            or name.split(" ", 1)[0] in DETAIL_OBJECT_PREFIXES
                        )
                    ]
                    LOGGER.info(
                        "Moonraker 扩展对象 %d 个：%s",
                        len(self._extra_objects),
                        "、".join(self._extra_objects),
                    )
            except Exception as exc:  # noqa: BLE001 - 拿不到就只看固定字段
                LOGGER.debug("objects/list 探测失败：%s", exc)
        return [*QUERY_OBJECTS, *self._extra_objects]

    def details(self) -> list[dict[str, str]]:
        """设备能提供但不在通用状态模型里的读数（风扇、传感器、主机负载…）。

        真机（Voron 2.4）实测有十几项，见 `docs/FIELD_NOTES.md` §2.2。
        """
        return list(self._details)

    def _fetch_frame(self) -> Optional[bytes]:
        """取主画面（``index 0``）。

        基类拿到它之后会存进 ``_latest_frame`` 并推进帧号，所以「主画面」这条通路
        与只有一路摄像头时完全一致。

        ⚠️ **主画面先取、其余画面后取**：反过来的话，一路坏掉的摄像头（没插上、
        或地址是 404）会把它那几次超时全部算在主画面之前 —— 用户看到的是
        "画面很卡甚至没有"。其余画面取不到不该影响能看的那一路。
        """
        if not self._cameras:
            self._discover_cameras()
            if not self._cameras:
                return None
        body = self._fetch_camera(0)
        self._fetch_extra_cameras()
        return body

    def _fetch_camera(self, index: int) -> Optional[bytes]:
        """取指定摄像头的快照；失败时在候选基准地址之间轮换一次再放弃。

        为什么要轮换：Moonraker 给的相对路径（``/webcam/…``）实际挂在**主机 80 端口**
        上（crowsnest / 前端静态目录），而 Moonraker 自己的端口上返回 404。
        但有些部署确实把摄像头挂在 Moonraker 端口后面，所以两个都试，
        谁成功就把结果记进 ``base``，之后不再重复试错。

        非主画面（``index > 0``）只试一次、用更短的超时：它坏掉不该拖慢主画面。
        """
        camera = self._camera_at(index)
        if camera is None:
            return None
        if camera["base"]:
            candidates = [camera["base"]]
        elif index == 0:
            candidates = list(self._candidate_bases())
        else:
            # 副画面不该自己去试两种基准：主画面已经告诉我们哪种能用了
            known = self._camera_base or (self._candidate_bases() or [""])[0]
            candidates = [known] if known else []
        timeout = self.timeout if index == 0 else min(self.timeout, EXTRA_CAMERA_TIMEOUT)
        last_detail = ""
        for base in candidates:
            url = self._resolve(camera["path"], base)
            body, detail = self._snapshot(url, timeout=timeout)
            if body:
                if camera["base"] != base:
                    camera["base"] = base
                    if index == 0:
                        self._camera_base = base
                camera["fails"] = 0
                camera["detail"] = ""
                return body
            last_detail = detail
        camera["fails"] = int(camera.get("fails", 0)) + 1
        camera["detail"] = last_detail
        if index == 0:
            self.last_camera_detail = last_detail
        return None

    def _snapshot(self, url: str, timeout: float = 0.0) -> tuple[Optional[bytes], str]:
        """下载一帧快照，返回 ``(JPEG 字节 或 None, 失败说明)``。"""
        try:
            request = urllib.request.Request(url)
            if self.api_key:
                request.add_header("X-Api-Key", self.api_key)
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                body = response.read()
                ctype = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            return None, f"摄像头 {url} 返回 HTTP {exc.code}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return None, f"摄像头不可用：{exc}"
        if not body.startswith(b"\xff\xd8"):
            return None, f"摄像头返回的不是 JPEG（{ctype or '无 Content-Type'}）：{url}"
        return body, ""

    def _fetch_extra_cameras(self) -> None:
        """顺带取其余摄像头的画面（失败次数多的先放一放，避免每轮都白等）。"""
        for camera in self._cameras[1:]:
            index = int(camera["index"])
            if int(camera.get("fails", 0)) >= EXTRA_CAMERA_MAX_FAILS and not camera.get(
                "probe_again"
            ):
                continue
            body = self._fetch_camera(index)
            if body:
                self._extra_seq += 1
                self._extra_frames[index] = (self._extra_seq, body)
                camera["probe_again"] = False
            elif int(camera.get("fails", 0)) == EXTRA_CAMERA_MAX_FAILS:
                # 连错几次就隔一会儿再试（摄像头可能只是还没启动）
                camera["probe_again"] = True

    def _candidate_bases(self) -> list[str]:
        """相对 URL 的候选基准地址：主机根优先，其次 Moonraker 端口。"""
        host = urllib.parse.urlsplit(self.base_url).hostname or str(getattr(self.info, "ip", ""))
        scheme = "https" if self.base_url.startswith("https://") else "http"
        root = f"{scheme}://{host}"
        bases = [self._camera_base] if self._camera_base else []
        for base in (root, self.base_url):
            if base and base not in bases:
                bases.append(base)
        return bases

    @staticmethod
    def _resolve(path: str, base: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{base.rstrip('/')}{path if path.startswith('/') else '/' + path}"

    def _camera_at(self, index: int) -> Optional[dict[str, Any]]:
        for camera in self._cameras:
            if int(camera["index"]) == int(index):
                return camera
        return None

    def _add_camera(self, name: str, location: str, path: str, stream_path: str = "") -> None:
        """登记一路摄像头（按 URL 去重）。``index`` 由加入顺序决定，0 是主画面。"""
        if not path:
            return
        for camera in self._cameras:
            if camera["path"] == path:
                return
        self._cameras.append(
            {
                "index": len(self._cameras),
                "name": name or f"摄像头 {len(self._cameras) + 1}",
                "location": location,
                "path": path,
                "stream_path": stream_path,
                "base": "",
                "fails": 0,
                "detail": "",
                "probe_again": False,
            }
        )
        if len(self._cameras) == 1:
            self._camera_url = self._resolve(path, self._candidate_bases()[-1] if not path.startswith("http") else "")
        self.capabilities = self.capabilities.merged(
            has_camera=True, video_channel="http_snapshot"
        )
        self.video_backend = "快照"

    def refresh_cameras(self) -> list[dict[str, Any]]:
        """重新发现摄像头并丢掉缓存帧（界面上「刷新视频流」按钮用这个）。

        场景：刚把摄像头插上 / 刚在 crowsnest 里加了一路 / 之前那路 502 现在起来了。
        只清空缓存不重建会话 —— 用户点一下就要立刻看到结果，不该等下一次轮询或重连。
        """
        self._camera_probed = False
        self._cameras = []
        self._extra_frames = {}
        self._camera_base = ""
        self._camera_url = self._user_camera_url
        self._discover_cameras()
        LOGGER.info("重新发现摄像头：%d 路（%s）", len(self._cameras), self.info.ip)
        return self.cameras()

    def cameras(self) -> list[dict[str, Any]]:
        """这台机器的摄像头清单（给界面做「选哪一路 / 同时看几路」）。

        每项包含：``index`` / ``name`` / ``location`` / ``available`` / ``detail`` /
        ``url``（解析后的快照地址，便于用户核对）。没有任何摄像头时返回空列表。
        """
        items = []
        for camera in self._cameras:
            base = camera["base"] or (self._candidate_bases()[0] if self._candidate_bases() else "")
            items.append(
                {
                    "index": int(camera["index"]),
                    "name": str(camera["name"]),
                    "location": str(camera.get("location", "")),
                    "available": int(camera.get("fails", 0)) == 0,
                    "detail": str(camera.get("detail", "")),
                    "url": self._resolve(str(camera["path"]), base) if base else str(camera["path"]),
                    "stream_url": (
                        self._resolve(str(camera["stream_path"]), base)
                        if camera.get("stream_path") and base
                        else ""
                    ),
                }
            )
        return items

    def latest_frame(self, camera: int = 0) -> tuple[int, Optional[bytes]]:
        """取某一路画面的最新帧。

        ``camera=0`` 走基类那条通路（主画面）；其它序号取本适配器缓存的那一路。
        多路摄像头是 Voron 这类机器的常态（喷嘴 + 舱内），
        界面据此做「切换看哪一路」或「同屏看几路」。
        """
        if int(camera) == 0:
            return super().latest_frame()
        with self._lock:
            seq, frame = self._extra_frames.get(int(camera), (0, None))
        return seq, frame

    def _discover_cameras(self) -> None:
        """用官方端点 ``/server/webcams/list`` 发现**全部**可用摄像头。

        只探测一次（失败则退化为"无画面"或只有用户手填的那一路），因为
        ``webcams/list`` 在设备端是数据库查询，没必要每轮都问。

        实测（Voron 2.4 + crowsnest，2026-09）：返回的是**相对路径**
        ``/webcam/?action=snapshot``，其中 ``/webcam/`` 由 nginx 代理到
        crowsnest 的 ustreamer，而 Moonraker 端口上没有这个路径（404）——
        以前这里把相对路径拼在 Moonraker 端口上，于是「有摄像头却永远没画面」。
        """
        if self._camera_probed:
            return
        self._camera_probed = True
        # 用户手填的地址优先当作主画面（例如 U1 那种 Moonraker 里没登记的摄像头）
        if self._camera_url:
            self._add_camera("自定义", "", self._camera_url)
        try:
            data = self._request("/server/webcams/list")
        except Exception as exc:  # noqa: BLE001 - 探测失败很常见，不值得报错
            LOGGER.debug("webcams/list 探测失败：%s", exc)
            if self._camera_url:
                self.capabilities = self.capabilities.merged(
                    has_camera=True, video_channel="http_snapshot"
                )
                self.video_backend = "快照"
            return
        webcams = data.get("result", {}).get("webcams") if isinstance(data, dict) else None
        if not isinstance(webcams, list):
            return
        for webcam in webcams:
            if not isinstance(webcam, dict):
                continue
            if webcam.get("enabled") is False:
                # 设备端明确禁用的不登记：它只会每次都失败
                continue
            # 优先用快照（单帧 JPEG），因为本程序按帧取用，不解析 MJPEG 流；
            # 流地址仍然保留，界面可以给用户一个「在浏览器里打开」的入口。
            snapshot = str(webcam.get("snapshot_url") or webcam.get("stream_url") or "")
            stream = str(webcam.get("stream_url") or "")
            self._add_camera(
                str(webcam.get("name") or ""),
                str(webcam.get("location") or ""),
                snapshot,
                stream if stream != snapshot else "",
            )
        if self._cameras:
            LOGGER.info(
                "发现 %d 路摄像头：%s",
                len(self._cameras),
                "、".join(f"{item['name']}" for item in self._cameras),
            )

    def _absolute(self, url: str) -> str:
        """把（可能是相对的）摄像头地址补全 —— 保留给外部调用方，内部用 :meth:`_resolve`。"""
        bases = self._candidate_bases()
        return self._resolve(url, bases[0] if bases else self.base_url)

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

        if not self._camera_url or self._keepalive_supported is False:
            return
        # 默认保活：对 U1 这类设备是"能否看到画面"的前提。
        # ⚠️ 但普通 Moonraker（Voron 实测）里**没有**这个方法，调用会返回
        # -32601 Method not found；以前每 5 秒试一次、每次都打一条告警，
        # 把真的问题淹掉了。所以这里一旦确认设备不认识它就永久关掉。
        self._ws_call(
            "camera.start_monitor",
            {"domain": "lan", "interval": 0},
            missing_method_disables="keepalive",
        )

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

    def _ws_call(
        self, method: str, params: Optional[dict] = None, *, missing_method_disables: str = ""
    ) -> bool:
        """发一条 WS JSON-RPC 调用；失败返回 False（不抛异常）。

        :param missing_method_disables: 填 ``"keepalive"`` 时，如果设备明确回答
            「方法不存在」（JSON-RPC ``-32601``），就把摄像头保活永久关掉 ——
            这是"这台设备不需要它"，不是故障，不该每 5 秒刷一条告警。
        """
        client = self._ensure_ws()
        if client is None:
            return False
        try:
            client.call(method, params)
            return True
        except (WebSocketError, OSError, ValueError) as exc:
            text = str(exc)
            if missing_method_disables and ("-32601" in text or "Method not found" in text):
                self._keepalive_supported = False
                LOGGER.info("设备不认识 %s，已关闭摄像头保活（这不是故障）", method)
                return False
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
