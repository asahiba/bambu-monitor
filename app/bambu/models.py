"""拓竹打印机机型识别、状态数据模型。

状态字段名参考 OpenBambuAPI 的 pushing.pushall / print.push_status 报文。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

from . import hms as hms_codes


class PrinterModel(str, Enum):
    """支持的机型。"""

    X1C = "X1C"
    X1 = "X1"
    X1E = "X1E"
    P1P = "P1P"
    P1S = "P1S"
    A1 = "A1"
    A1MINI = "A1 mini"
    P2S = "P2S"
    H2D = "H2D"
    H2S = "H2S"
    X2D = "X2D"
    UNKNOWN = "未知机型"

    def __str__(self) -> str:  # pragma: no cover - 仅用于展示
        return self.value

    @property
    def label(self) -> str:
        return self.value

    @property
    def supports_rtsp(self) -> bool:
        """X1/P2S/H2/X2D 系列带本地 RTSPS(322) 服务，A1/P1 只有 6000 端口 JPEG 流。"""
        return self in (
            PrinterModel.X1C,
            PrinterModel.X1,
            PrinterModel.X1E,
            PrinterModel.P2S,
            PrinterModel.H2D,
            PrinterModel.H2S,
            PrinterModel.X2D,
        )

    @property
    def has_chamber_sensor(self) -> bool:
        """是否有腔温传感器。

        A1 / A1 mini / P1P / P1S 没有腔温探头，固件仍会报送一个无效值（实测 P1S 常年是 5.0℃），
        直接显示会误导用户，因此这些机型不显示仓温。
        """
        return self in (
            PrinterModel.X1C,
            PrinterModel.X1,
            PrinterModel.X1E,
            PrinterModel.X2D,
            PrinterModel.H2D,
            PrinterModel.H2S,
            PrinterModel.P2S,
        )

    @property
    def video_channel(self) -> str:
        """该机型可用的视频通道（"rtsp" / "tcp6000" / "auto"）。

        实测（真机 + 正确访问代码）：

        * P1S（固件 01.10.00.00）：6000 端口 1.8 秒出画面，322 端口不开放 → 只能用 6000；
        * X2D（固件 01.01.01.00）：322 端口 3.1 秒出画面，6000 端口即使口令正确也会回
          一个「拒绝」包 → 只能用 RTSPS；
        * X1C（固件 01.11.02.00）：322 端口 1.5 秒出画面，6000 端口同样被拒；
          但更老的 X1 固件是靠 6000 端口取画面的，所以这里返回 auto：
          优先 RTSPS，失败再退 6000，并由看门狗在两种情况下自动纠正。
        """
        if self in (
            PrinterModel.A1,
            PrinterModel.A1MINI,
            PrinterModel.P1P,
            PrinterModel.P1S,
        ):
            return "tcp6000"
        if self in (PrinterModel.X2D, PrinterModel.H2D, PrinterModel.H2S, PrinterModel.P2S):
            return "rtsp"
        # X1 / X1C / X1E 以及未知机型：先 RTSPS，失败再退 6000
        return "auto"


#: 序列号前缀 -> 机型（依据社区逆向资料，用于自动识别）
SERIAL_PREFIX_MODEL: dict[str, PrinterModel] = {
    "00M": PrinterModel.X1C,
    "00W": PrinterModel.X1,
    "03W": PrinterModel.X1E,
    "01S": PrinterModel.P1P,
    "01P": PrinterModel.P1S,
    "030": PrinterModel.A1MINI,
    "039": PrinterModel.A1,
    "22E": PrinterModel.P2S,
    "093": PrinterModel.H2S,
    "094": PrinterModel.H2D,
    "20P": PrinterModel.X2D,
}

#: 型号名关键字（大小写不敏感）-> 机型，用于解析 SSDP 的 devmodel/devname 字段
MODEL_NAME_HINTS: list[tuple[str, PrinterModel]] = [
    ("x2d", PrinterModel.X2D),
    ("x1carbon", PrinterModel.X1C),
    ("x1c", PrinterModel.X1C),
    ("x1e", PrinterModel.X1E),
    ("x1", PrinterModel.X1),
    ("p2s", PrinterModel.P2S),
    ("p1s", PrinterModel.P1S),
    ("p1p", PrinterModel.P1P),
    ("a1mini", PrinterModel.A1MINI),
    ("a1 mini", PrinterModel.A1MINI),
    ("a1m", PrinterModel.A1MINI),
    ("a1", PrinterModel.A1),
    ("h2d", PrinterModel.H2D),
    ("h2s", PrinterModel.H2S),
]


def _normalize_model_text(text: str) -> str:
    """把机型描述压成便于子串匹配的形式：去掉空白与 ``-``/``_``、转小写。

    真机的 SSDP 字段经常带空格（例如 ``Bambu Lab X1 Carbon``），
    只匹配 ``x1carbon`` 会漏掉它，进而退化成匹配到 ``x1``（把 X1C 认成 X1）。
    """
    return "".join(text.strip().lower().split()).replace("-", "").replace("_", "")


def detect_model(serial: str = "", model_name: str = "") -> PrinterModel:
    """综合 SSDP 型号名与序列号前缀推断机型。

    型号名匹配时会忽略空格与 ``-``/``_``，因此 ``X1 Carbon``、``X1-Carbon``、
    ``x1carbon`` 都能识别为 X1C（关键字表的顺序保证先匹配更具体的型号）。
    """
    text = _normalize_model_text(model_name)
    if text:
        for keyword, model in MODEL_NAME_HINTS:
            if _normalize_model_text(keyword) in text:
                return model
    sn = (serial or "").strip().upper()
    if len(sn) >= 3:
        model = SERIAL_PREFIX_MODEL.get(sn[:3])
        if model is not None:
            return model
    return PrinterModel.UNKNOWN


#: gcode_state -> 中文状态
STATE_TEXT = {
    "IDLE": "空闲",
    "RUNNING": "打印中",
    "PAUSE": "已暂停",
    "PAUSED": "已暂停",
    "FINISH": "打印完成",
    "FAILED": "打印失败",
    "PREPARE": "准备中",
    "SLICING": "切片中",
    "INIT": "初始化",
    "OFFLINE": "离线",
    "UNKNOWN": "未知",
}


@dataclass
class AmsTray:
    """一个料卷槽位（AMS 里的某一格，或外挂料盘）。"""

    ams_id: int = 0
    tray_id: int = 0
    tray_type: str = ""
    color: str = ""  # RRGGBBAA
    remain: int = -1  # -1 表示未知
    nozzle_temp_min: int = 0
    nozzle_temp_max: int = 0
    empty: bool = False
    external: bool = False
    #: 是否有 RFID 标签（非官方料卷 / 未接 AMS 时读不到真实余量）
    has_rfid: bool = False

    @property
    def color_hex(self) -> str:
        raw = (self.color or "").strip().lstrip("#")
        if len(raw) >= 6:
            return "#" + raw[:6].upper()
        return ""

    @property
    def remain_known(self) -> bool:
        """余量是否可信。

        只有带 RFID 标签的官方料卷才会被真正称重/计数：
        * 第三方料卷（tag_uid 全 0）：固件常常直接报 100 或 0，属于假值；
        * 外挂料盘：没有余量检测机制；
        这两种情况都不能显示余量，否则会误导用户。
        """
        if self.external or self.empty:
            return False
        if not self.has_rfid:
            return False
        return self.remain >= 0

    @property
    def remain_text(self) -> str:
        """余量文案；读不到时返回空字符串（调用方应直接不显示）。"""
        if self.empty:
            return "空"
        if not self.remain_known:
            return ""
        return f"{self.remain}%"

    @property
    def remain_hint(self) -> str:
        if self.empty:
            return "槽位为空"
        if self.remain_known:
            return f"余量 {self.remain}%"
        if self.external:
            return "余量未知（外挂料盘没有余量检测）"
        return "余量未知（非官方料卷 / 无 RFID 标签，固件上报的数值不可信）"

    @property
    def label(self) -> str:
        if self.external:
            return "外挂"
        return f"AMS{self.ams_id + 1}-{self.tray_id + 1}"

    @property
    def summary(self) -> str:
        parts = [self.tray_type or "未知"]
        if self.color_hex:
            parts.append(self.color_hex)
        text = self.remain_text
        if text:
            parts.append(text)
        return " ".join(parts)


@dataclass
class HmsItem:
    """一条 HMS 提示/故障。"""

    attr: int = 0
    code: int = 0
    text: str = ""
    wiki: str = ""
    short_code: str = ""

    @property
    def display(self) -> str:
        if self.text:
            return f"{self.short_code} {self.text}" if self.short_code else self.text
        return self.short_code or f"{self.attr:08X}{self.code:08X}"


@dataclass
class PrinterInfo:
    """一台打印机的静态信息（来自自动搜索或手动录入）。"""

    ip: str = ""
    serial: str = ""
    name: str = ""
    model: PrinterModel = PrinterModel.UNKNOWN
    firmware: str = ""
    access_code: str = ""
    #: 视频传输方式：auto / tcp6000 / rtsp
    stream_mode: str = "auto"
    #: 画面占比：1 = 普通（1 格），2 = 重点画面（占 2×2 格）
    tile_span: int = 1
    discovered: bool = False

    def display_name(self) -> str:
        if self.name:
            return self.name
        if self.serial:
            return self.serial[-6:]
        return self.ip or "未命名"


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


@dataclass
class PrinterStatus:
    """打印机实时状态。

    由 MQTT `device/<序列号>/report` 报文增量更新（P1 系列只推送变化过的字段，
    因此这里必须做字段合并而不是覆盖）。
    """

    # 连接状态
    mqtt_online: bool = False
    camera_online: bool = False
    last_message_ts: float = 0.0
    last_error: str = ""

    # 打印任务
    gcode_state: str = ""
    progress: int = 0
    remaining_minutes: int = 0
    layer_num: int = 0
    total_layer_num: int = 0
    subtask_name: str = ""
    print_error: int = 0
    hms_count: int = 0

    # 温度
    nozzle_temper: float = 0.0
    nozzle_target_temper: float = 0.0
    nozzle_temper_2: Optional[float] = None
    nozzle_target_temper_2: Optional[float] = None
    bed_temper: float = 0.0
    bed_target_temper: float = 0.0
    chamber_temper: Optional[float] = None

    # 耗材（AMS + 外挂）
    ams_trays: list[AmsTray] = field(default_factory=list)
    ams_exist: bool = False
    vt_tray: Optional[AmsTray] = None
    tray_now: int = 255
    tray_tar: int = 255

    # 故障与灯光
    hms_items: list[HmsItem] = field(default_factory=list)
    print_error_text: str = ""
    lights: dict[str, str] = field(default_factory=dict)

    # 其它
    wifi_signal: str = ""
    rtsp_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def state_text(self) -> str:
        state = (self.gcode_state or "").upper()
        if not state:
            return "未知"
        return STATE_TEXT.get(state, self.gcode_state)

    @property
    def is_printing(self) -> bool:
        return (self.gcode_state or "").upper() in ("RUNNING", "PAUSE", "PAUSED", "PREPARE")

    @property
    def is_paused(self) -> bool:
        return (self.gcode_state or "").upper() in ("PAUSE", "PAUSED")

    @property
    def remaining_text(self) -> str:
        minutes = max(0, self.remaining_minutes)
        if minutes <= 0:
            return "--"
        hours, mins = divmod(minutes, 60)
        if hours:
            return f"{hours}小时{mins:02d}分"
        return f"{mins}分钟"

    @property
    def finish_time_text(self) -> str:
        """预计完成时间（本地时钟）。"""
        minutes = max(0, self.remaining_minutes)
        if minutes <= 0 or not self.is_printing:
            return "--"
        eta = datetime.now() + timedelta(minutes=minutes)
        # 跨天时标注「明天」
        if eta.date() != datetime.now().date():
            return eta.strftime("明天 %H:%M")
        return eta.strftime("%H:%M")

    @property
    def wifi_level(self) -> int:
        """把 ``-45dBm`` 之类的字符串换算成 0-4 格信号。"""
        text = (self.wifi_signal or "").lower().replace("dbm", "").strip()
        try:
            dbm = float(text)
        except ValueError:
            return 0
        if dbm >= -55:
            return 4
        if dbm >= -65:
            return 3
        if dbm >= -75:
            return 2
        if dbm >= -85:
            return 1
        return 0

    @property
    def light_on(self) -> Optional[bool]:
        """舱灯状态（没有灯光信息时返回 None）。"""
        for node in ("chamber_light", "chamber_light2"):
            mode = (self.lights.get(node) or "").lower()
            if mode:
                return mode in ("on", "flashing")
        return None

    @property
    def ams_by_unit(self) -> dict[int, list[AmsTray]]:
        units: dict[int, list[AmsTray]] = {}
        for tray in self.ams_trays:
            units.setdefault(tray.ams_id, []).append(tray)
        for trays in units.values():
            trays.sort(key=lambda item: item.tray_id)
        return dict(sorted(units.items()))

    @property
    def active_tray(self) -> Optional[AmsTray]:
        """当前正在使用的料卷。"""
        if self.tray_now in (254, 255):
            return self.vt_tray if self.tray_now == 254 else None
        ams_id, tray_id = divmod(int(self.tray_now), 4)
        for tray in self.ams_trays:
            if tray.ams_id == ams_id and tray.tray_id == tray_id:
                return tray
        return None

    @property
    def hms_count_actual(self) -> int:
        return len(self.hms_items)

    @property
    def stale(self) -> bool:
        """超过 30 秒没有收到报文则认为状态过期。"""
        if self.last_message_ts <= 0:
            return True
        return (time.time() - self.last_message_ts) > 30

    def apply_report(self, payload: dict[str, Any]) -> bool:
        """合并一条 MQTT 报文（{"print": {...}}），返回是否更新了状态。"""
        print_obj = payload.get("print")
        if not isinstance(print_obj, dict):
            return False

        self.raw.update(print_obj)
        src = self.raw

        self.gcode_state = str(src.get("gcode_state", self.gcode_state) or "")
        self.progress = _as_int(src.get("mc_percent"), self.progress)
        self.remaining_minutes = _as_int(src.get("mc_remaining_time"), self.remaining_minutes)
        self.layer_num = _as_int(src.get("layer_num"), self.layer_num)
        self.total_layer_num = _as_int(src.get("total_layer_num"), self.total_layer_num)
        self.subtask_name = str(src.get("subtask_name", self.subtask_name) or "")
        self.print_error = _as_int(src.get("print_error"), self.print_error)

        hms = src.get("hms")
        if isinstance(hms, list):
            self.hms_items = _parse_hms(hms)
            self.hms_count = len(self.hms_items)
        self.print_error_text = hms_codes.error_text(self.print_error) if self.print_error else ""
        lights = src.get("lights_report")
        if isinstance(lights, list):
            self.lights = {
                str(item.get("node", "")): str(item.get("mode", ""))
                for item in lights
                if isinstance(item, dict) and item.get("node")
            }

        self.nozzle_temper = _as_float(src.get("nozzle_temper"), self.nozzle_temper)
        self.nozzle_target_temper = _as_float(
            src.get("nozzle_target_temper"), self.nozzle_target_temper
        )
        # H2D 等双喷嘴机型会额外给出第二路温度
        if "nozzle_temper_2" in src:
            self.nozzle_temper_2 = _as_float(src.get("nozzle_temper_2"))
        if "nozzle_target_temper_2" in src:
            self.nozzle_target_temper_2 = _as_float(src.get("nozzle_target_temper_2"))
        self.bed_temper = _as_float(src.get("bed_temper"), self.bed_temper)
        self.bed_target_temper = _as_float(src.get("bed_target_temper"), self.bed_target_temper)
        if "chamber_temper" in src:
            self.chamber_temper = _as_float(src.get("chamber_temper"))

        # 耗材：AMS 各槽位 + 外挂料盘（tray_now/tray_tar 在 ams 对象里）
        ams_obj = src.get("ams")
        if isinstance(ams_obj, dict):
            self.ams_trays = _parse_ams(ams_obj)
            self.ams_exist = str(ams_obj.get("ams_exist_bits", "0")).strip("0") != "" or bool(
                self.ams_trays
            )
            self.tray_now = _as_int(ams_obj.get("tray_now"), self.tray_now)
            self.tray_tar = _as_int(ams_obj.get("tray_tar"), self.tray_tar)
        elif "ams" in src and ams_obj is None:
            self.ams_trays = []
            self.ams_exist = False
        vt = src.get("vt_tray")
        if isinstance(vt, dict):
            self.vt_tray = _parse_tray(vt, ams_id=255, external=True)
        # 少数固件把 tray_now 放在顶层
        if "tray_now" in src:
            self.tray_now = _as_int(src.get("tray_now"), self.tray_now)
        if "tray_tar" in src:
            self.tray_tar = _as_int(src.get("tray_tar"), self.tray_tar)

        self.wifi_signal = str(src.get("wifi_signal", self.wifi_signal) or "")
        ipcam = src.get("ipcam")
        if isinstance(ipcam, dict):
            rtsp = str(ipcam.get("rtsp_url", "") or "")
            if rtsp:
                self.rtsp_url = rtsp

        self.last_message_ts = time.time()
        return True


def _parse_tray(data: dict[str, Any], ams_id: int, external: bool = False) -> AmsTray:
    tray_type = str(data.get("tray_type", "") or "")
    color = str(data.get("tray_color", "") or "")
    remain = _as_int(data.get("remain"), -1)
    empty = not tray_type and not color.strip("0")
    # 有 RFID 标签才说明是能被真正计量的官方料卷
    tag_uid = str(data.get("tag_uid", "") or "").strip()
    tray_uuid = str(data.get("tray_uuid", "") or "").strip()
    has_rfid = bool(tag_uid.strip("0")) or bool(tray_uuid.strip("0"))
    return AmsTray(
        ams_id=ams_id,
        tray_id=_as_int(data.get("id"), 0),
        tray_type=tray_type,
        color=color,
        remain=remain if remain >= 0 else -1,
        nozzle_temp_min=_as_int(data.get("nozzle_temp_min"), 0),
        nozzle_temp_max=_as_int(data.get("nozzle_temp_max"), 0),
        empty=empty,
        external=external,
        has_rfid=has_rfid,
    )


def _parse_ams(ams_obj: dict[str, Any]) -> list[AmsTray]:
    trays: list[AmsTray] = []
    units = ams_obj.get("ams")
    if not isinstance(units, list):
        return trays
    for unit in units:
        if not isinstance(unit, dict):
            continue
        ams_id = _as_int(unit.get("id"), 0)
        for tray in unit.get("tray") or []:
            if isinstance(tray, dict):
                trays.append(_parse_tray(tray, ams_id=ams_id))
    return trays


def _parse_hms(items: list[Any]) -> list[HmsItem]:
    parsed: list[HmsItem] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        attr = _as_int(item.get("attr"), 0)
        code = _as_int(item.get("code"), 0)
        parsed.append(
            HmsItem(
                attr=attr,
                code=code,
                text=hms_codes.hms_text(attr, code),
                wiki=hms_codes.wiki_url(attr, code),
                short_code=hms_codes.display_code(attr, code),
            )
        )
    return parsed
