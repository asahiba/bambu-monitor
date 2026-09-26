"""拓竹打印机机型识别、状态数据模型。

状态字段名参考 OpenBambuAPI 的 pushing.pushall / print.push_status 报文。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from . import hms as hms_codes

if TYPE_CHECKING:  # 只用于类型标注：运行时不导入，避免与 app.core 形成环
    from ..core.capabilities import DeviceCapabilities


class PrinterModel(str, Enum):
    """支持的机型。

    新增机型时要同步 5 个地方（顺序无要求，但漏一个就会出现「识别不出来」或
    「走错视频通道」）：
    ``PrinterModel`` 枚举、``supports_rtsp``、``has_chamber_sensor``、
    ``video_channel``、``SERIAL_PREFIX_MODEL`` / ``MODEL_NAME_HINTS``。
    另外记得补 ``tests/test_models.py`` 的契约断言。
    """

    X1C = "X1C"
    X1 = "X1"
    X1E = "X1E"
    P1P = "P1P"
    P1S = "P1S"
    A1 = "A1"
    A1MINI = "A1 mini"
    #: A2L：实测机型（见 docs/FIELD_NOTES.md）。8883 + 6000 可用、322 不可达，
    #: 因此与 A1/P1 同属「6000 端口机型」；固件 01.01.00.00。
    #: 官方 2026-06-01 发布，单喷嘴、无腔温传感器（官方规格）。
    A2L = "A2L"
    P2S = "P2S"
    H2D = "H2D"
    #: H2D Pro：官方序列号前缀 `239`；SSDP 代号未确定（见 DEV_MODEL_CODES）。
    H2D_PRO = "H2D Pro"
    H2S = "H2S"
    #: H2C：官方 2025-11-18 发布，序列号前缀 `31B`，双喷嘴 + 6 位喷嘴架（Vortek），
    #: 有腔温传感器与有源腔温加热器（≤65℃），视频走 RTSPS(322)/H.264。
    #: SSDP 有两个代号：`O1C`（单喷嘴变体）与 `O1C2`（双喷嘴变体），两者都要认。
    H2C = "H2C"
    X2D = "X2D"
    UNKNOWN = "未知机型"

    def __str__(self) -> str:  # pragma: no cover - 仅用于展示
        return self.value

    @property
    def label(self) -> str:
        return self.value

    @property
    def is_known(self) -> bool:
        """机型是否已识别。

        替代散落各处的 ``model.value != "未知机型"`` 魔术字符串比较
        （``main_window`` / ``discover_dialog`` / ``discovery`` / ``headless`` 都曾这么写，
        改机型文案时会静默失效）。
        """
        return self is not PrinterModel.UNKNOWN

    @property
    def supports_rtsp(self) -> bool:
        """是否提供本地 RTSPS(322) 服务。

        X1/P2S/H2/X2D 系列带 RTSPS；A1/P1 系列**以及实测的 A2L** 只有 6000 端口 JPEG 流。
        """
        return self in (
            PrinterModel.X1C,
            PrinterModel.X1,
            PrinterModel.X1E,
            PrinterModel.P2S,
            PrinterModel.H2D,
            PrinterModel.H2D_PRO,
            PrinterModel.H2S,
            PrinterModel.H2C,
            PrinterModel.X2D,
        )

    @property
    def has_chamber_sensor(self) -> bool:
        """是否有腔温传感器。

        A1 / A1 mini / P1P / P1S **以及 A2L** 没有腔温探头，固件仍会报送一个无效值
        （实测 P1S 与 A2L 都常年是 5.0℃），直接显示会误导用户，因此这些机型不显示仓温。
        """
        return self in (
            PrinterModel.X1C,
            PrinterModel.X1,
            PrinterModel.X1E,
            PrinterModel.X2D,
            PrinterModel.H2D,
            PrinterModel.H2D_PRO,
            PrinterModel.H2S,
            PrinterModel.H2C,
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
        * A2L（固件 01.01.00.00）：8883 与 6000 开放、**322 不可达**（实测见
          docs/FIELD_NOTES.md）→ 与 A1/P1 同型，固定 6000，不去白试 RTSPS。
          ⚠️ 不能因为它是「A 系列新品」就猜 322：调研与源码断言都确认 A2L 走 6000。
        * H2C / H2D Pro：与 H2 系同族，走 RTSPS(322)/H.264。
        """
        if self in (
            PrinterModel.A1,
            PrinterModel.A1MINI,
            PrinterModel.A2L,
            PrinterModel.P1P,
            PrinterModel.P1S,
        ):
            return "tcp6000"
        if self in (
            PrinterModel.X2D,
            PrinterModel.H2D,
            PrinterModel.H2D_PRO,
            PrinterModel.H2S,
            PrinterModel.H2C,
            PrinterModel.P2S,
        ):
            return "rtsp"
        # X1 / X1C / X1E 以及未知机型：先 RTSPS，失败再退 6000
        return "auto"

    @property
    def has_enclosure_light(self) -> bool:
        """**猜测**该机型是否有可控的舱灯。

        ⚠️ 这只是猜测，**不要用它决定界面上灯按钮的显隐** ——
        请用 `PrinterStatus.light_on`（设备真的上报了 `lights_report` 才算数）。
        本属性只在与设备还没建立遥测时作为兜底提示。

        教训：A2L 是开放式机型，我据此推断它没有舱灯，但**实测它上报了
        `{"node": "chamber_light", "mode": "off"}`** —— 于是界面把灯按钮藏了，
        用户无法开关灯。机型规格推不出灯光能力，只有设备上报才算数。
        """
        return self in (
            PrinterModel.X1C,
            PrinterModel.X1,
            PrinterModel.X1E,
            PrinterModel.X2D,
            PrinterModel.P1P,
            PrinterModel.P1S,
            PrinterModel.P2S,
            PrinterModel.H2D,
            PrinterModel.H2D_PRO,
            PrinterModel.H2S,
            PrinterModel.H2C,
            # 实测有 chamber_light（见上）
            PrinterModel.A2L,
        )

    @property
    def capabilities(self) -> "DeviceCapabilities":
        """该机型「能做什么」。

        界面与网页应当读 ``PrinterSession.capabilities``（它会把运行时观测叠加进来），
        只有拿不到会话时才退而读这里的机型固有能力。
        """
        from ..core.capabilities import DeviceCapabilities

        return DeviceCapabilities(
            video_channel=self.video_channel,
            has_camera=True,
            has_chamber_sensor=self.has_chamber_sensor,
            nozzle_count=2 if self in (PrinterModel.H2D, PrinterModel.H2D_PRO, PrinterModel.H2C, PrinterModel.X2D) else 1,
            has_wifi_signal=True,
            supports_ams=True,
            supports_external_spool=True,
            can_pause=True,
            can_stop=True,
            can_control_light=self.has_enclosure_light,
            can_set_speed=True,
            has_hms=True,
        )


#: 序列号前缀 -> 机型。
#:
#: **来源：拓竹官方 Wiki**《如何查找序列号》
#: https://wiki.bambulab.com/en/general/find-sn —— 逐机型明写前 3 位，
#: 不再是社区逆向；A2L 的 ``26A`` 另有本机真机实测交叉验证（docs/FIELD_NOTES.md）。
#:
#: ⚠️ **官方同一页面的警告**：更换 AP 板/主板后，机器实际序列号会与机身贴纸不同。
#: 因此识别判据的优先级必须是 **devmodel 代号 > 序列号前缀 > 型号名关键字**，
#: 不能只靠序列号（见 ``DEV_MODEL_CODES``）。
#:
#: 未见于官方表的前缀一律不猜：猜错会让用户看到错误的机型与错误的视频通道，
#: 而「未知机型」只是少显示一个名字、走 auto 通道仍能出画面，代价小得多。
SERIAL_PREFIX_MODEL: dict[str, PrinterModel] = {
    "00M": PrinterModel.X1C,
    "03W": PrinterModel.X1E,
    "01S": PrinterModel.P1P,
    "01P": PrinterModel.P1S,
    "030": PrinterModel.A1MINI,
    "039": PrinterModel.A1,
    "26A": PrinterModel.A2L,  # 官方表 + 本机实测 26A00A000000000000
    "22E": PrinterModel.P2S,
    "093": PrinterModel.H2S,
    "094": PrinterModel.H2D,
    "239": PrinterModel.H2D_PRO,
    "31B": PrinterModel.H2C,
    "20P": PrinterModel.X2D,
    # `00W` 是 X1（无后缀）—— **官方表未列**，仅社区论坛与逆向源码给出，
    # 未经真机验证。保留是因为猜错的代价仅为「显示成 X1」（两者同属 RTSPS 型），
    # 若日后确认有误，删掉这一行即可。
    "00W": PrinterModel.X1,
}

#: SSDP 的 `devmodel.bambu.com` 代号 -> 机型。
#:
#: 这些代号与型号名毫无字面关系（P1S 是 ``C12``、A2L 是 ``N9``），因此**必须单独成表**：
#: 只靠型号名关键字匹配会全部落空，退化成「未知机型」→ 走 auto 通道 → 新机型可能被
#: 错误地先去试 6000 端口并陷入重连循环。
#:
#: 可信度：``C11``/``C12``/``N9``/``O1C``/``O1C2`` 有官方源码或社区实测支撑；
#: ``O1E``/``O2D``（H2D Pro）**来源不一致、未确定**，两个都收进来，
#: 命中任意一个即可识别为 H2D Pro（同型，能力一致，猜错无副作用）。
DEV_MODEL_CODES: dict[str, PrinterModel] = {
    "c11": PrinterModel.P1P,
    "c12": PrinterModel.P1S,
    "n9": PrinterModel.A2L,
    "o1c2": PrinterModel.H2C,  # ⚠️ 必须排在 "o1c" 之前（双喷嘴变体）
    "o1c": PrinterModel.H2C,
    "o1e": PrinterModel.H2D_PRO,
    "o2d": PrinterModel.H2D_PRO,
}

#: 型号名关键字（大小写不敏感）-> 机型，用于解析 SSDP 的 devmodel/devname 字段。
#:
#: **顺序即优先级**：更具体的型号必须排在更宽泛的前面（例如 ``x1carbon`` 在 ``x1`` 之前、
#: ``a2l`` 在 ``a1`` 之前、``h2dpro`` 在 ``h2d`` 之前），否则会被短关键字抢先命中。
MODEL_NAME_HINTS: list[tuple[str, PrinterModel]] = [
    ("x2d", PrinterModel.X2D),
    ("x1carbon", PrinterModel.X1C),
    ("x1c", PrinterModel.X1C),
    ("x1e", PrinterModel.X1E),
    ("x1", PrinterModel.X1),
    ("p2s", PrinterModel.P2S),
    ("p1s", PrinterModel.P1S),
    ("p1p", PrinterModel.P1P),
    ("a2l", PrinterModel.A2L),
    ("a1mini", PrinterModel.A1MINI),
    ("a1 mini", PrinterModel.A1MINI),
    ("a1m", PrinterModel.A1MINI),
    ("a1", PrinterModel.A1),
    ("h2dpro", PrinterModel.H2D_PRO),
    ("h2d pro", PrinterModel.H2D_PRO),
    ("h2d", PrinterModel.H2D),
    ("h2s", PrinterModel.H2S),
    ("h2c", PrinterModel.H2C),
]


def _normalize_model_text(text: str) -> str:
    """把机型描述压成便于子串匹配的形式：去掉空白与 ``-``/``_``、转小写。

    真机的 SSDP 字段经常带空格（例如 ``Bambu Lab X1 Carbon``），
    只匹配 ``x1carbon`` 会漏掉它，进而退化成匹配到 ``x1``（把 X1C 认成 X1）。
    """
    return "".join(text.strip().lower().split()).replace("-", "").replace("_", "")


def detect_model(
    serial: str = "", model_name: str = "", dev_model: str = ""
) -> PrinterModel:
    """综合 SSDP 字段与序列号推断机型。

    判据优先级（**不要随意调换**）：

    1. ``model_name``（``devname``）里的型号名关键字 —— 最明确。匹配时忽略空格与
       ``-``/``_``，因此 ``X1 Carbon`` / ``X1-Carbon`` / ``x1carbon`` 都识别为 X1C；
       关键字表顺序保证更具体的型号优先。
       先试它是因为老机型的 ``devmodel`` 直接就报完整型号名（例如 ``P1S``），
       比两个字符的代号更不容易误伤。
    2. ``dev_model``（``devmodel.bambu.com`` 代号）—— 新机型才用代号，且代号与型号名
       毫无字面关系（P1S 是 ``C12``、A2L 是 ``N9``、H2C 是 ``O1C``/``O1C2``），
       所以必须单独查 ``DEV_MODEL_CODES`` 表，不能用关键字匹配。
    3. 序列号前 3 位 —— **兜底**。官方明确说明：更换 AP 板/主板后序列号会变，
       所以它排在最后。
    """
    text = _normalize_model_text(model_name)
    if text:
        for keyword, model in MODEL_NAME_HINTS:
            if _normalize_model_text(keyword) in text:
                return model
    code = _normalize_model_text(dev_model)
    if code:
        model = DEV_MODEL_CODES.get(code)
        if model is not None:
            return model
        # 有些固件把完整型号名填在 devmodel 字段里，再按关键字试一次
        for keyword, hinted in MODEL_NAME_HINTS:
            if _normalize_model_text(keyword) in code:
                return hinted
    sn = (serial or "").strip().upper()
    if len(sn) >= 3:
        model = SERIAL_PREFIX_MODEL.get(sn[:3])
        if model is not None:
            return model
    return PrinterModel.UNKNOWN


#: gcode_state -> 中文状态
STATE_TEXT = {    "IDLE": "空闲",
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

#: MQTT `fun` 字段里表示「MQTT 命令需签名校验」的位。
#:
#: 置位意味着**必须在打印机触屏上开启 Developer Mode**，否则第三方下发的控制命令
#: 会被静默忽略（机器回一条 HMS 0500-0500-0001-0007），界面上表现为「点了没反应」，
#: 而画面与遥测完全正常。新机型（H2C / H2S / X2D / P2S / A2L）默认就需要它。
#: 实测 A2L（固件 01.01.05.00）的 `fun` = "100d122002fbd"，该位**已置位**。
MQTT_SIGNATURE_REQUIRED = 0x20000000


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
    #: 设备族 id（见 `app/core/registry.py`）。**空字符串 = 拓竹**：
    #: 历史上写出的所有配置都没有这个字段，`resolve_family()` 据此保证老配置免迁移。
    family: str = ""
    #: 服务端口。0 = 用该族的默认端口（拓竹是 8883，Moonraker 是 80）。
    port: int = 0
    #: 非拓竹族的凭据（Moonraker 的 API Key）。与 ``access_code`` 一样按本机方式
    #: 加密保存；留空表示不需要（内网 Moonraker 默认免鉴权）。
    api_key: str = ""
    #: 第三方族的摄像头地址（留空则尝试自动发现）。U1 这类设备的摄像头端点
    #: 不是标准 MJPEG，需要用户手填 —— 见 `docs/FIELD_NOTES.md`。
    camera_url: str = ""
    #: 这台设备**显示哪一路**画面（多摄像头机器用，例如 Voron 的喷嘴 + 舱内）。
    #: 0 = 第一路。可选的摄像头列表见 `session.cameras()`。
    camera_index: int = 0
    #: 第三方族的「开灯 / 关灯」G-code。留空表示不声明灯控能力。
    #:
    #: 为什么让用户自己填：Klipper 机器上"舱灯"没有统一做法 —— 可能是宏
    #: （``LIGHT_ON``）、可能是输出引脚（``SET_PIN PIN=caselight VALUE=1``）、
    #: 也可能接在某个风扇上（``SET_FAN_SPEED FAN=chamber_light SPEED=1``）。
    #: 猜名字必然踩空，所以按 Fluidd / Mainsail 的做法让用户填**命令**。
    light_on_gcode: str = ""
    light_off_gcode: str = ""

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


def _as_hex_int(value: Any) -> Optional[int]:
    """把十六进制**字符串**形式的字段解析成整数（拓竹的 ``fun`` 字段就是这样）。

    实测 A2L（固件 01.01.05.00）上报 ``"fun": "100d122002fbd"`` —— 是字符串而不是数字，
    用 ``_as_int`` 会直接失败退化成 0，从而漏掉「需要 Developer Mode」这个关键信息。
    这里同时容忍整数输入（老机型/其它固件可能直接给数字）。
    无法解析时返回 ``None``，由调用方决定回退策略。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    # 允许带 0x 前缀；不含前缀时按十六进制解析（拓竹的格式）
    if text.startswith("0x"):
        text = text[2:]
    try:
        return int(text, 16)
    except ValueError:
        return None


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
    #: 固件功能位（报文里的 `fun` 字段，是**十六进制字符串**，例如 "100d122002fbd"）。
    #: None 表示该机型/固件没有上报这个字段。
    fun_bits: Optional[int] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_mqtt_signature(self) -> Optional[bool]:
        """控制命令是否需要 MQTT 签名（即是否需要开 Developer Mode）。

        `fun` 的 bit ``0x20000000`` 被置位表示「MQTT 命令需签名校验」。新机型
        （H2C / H2S / X2D / P2S / **A2L**）默认就需要它：**未在打印机触屏上开启
        Developer Mode 时，第三方下发的控制命令会被静默忽略**，界面上表现为
        「点了暂停没反应」，而画面与遥测一切正常。

        返回 ``None`` 表示无从判断（没上报 `fun`）——此时不要据此禁用按钮，
        否则会把老机型也误伤。
        """
        if self.fun_bits is None:
            return None
        return bool(self.fun_bits & MQTT_SIGNATURE_REQUIRED)

    @property
    def developer_mode(self) -> Optional[bool]:
        """是否**已开启** Developer Mode（= 不需要签名）。``None`` 表示未知。"""
        needs = self.needs_mqtt_signature
        return None if needs is None else (not needs)

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
        # `fun` 是十六进制字符串，表示固件功能位；bit 0x20000000 关系到能否下发控制命令
        if "fun" in src:
            self.fun_bits = _as_hex_int(src.get("fun"))
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
