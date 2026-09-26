"""设备无关的状态模型。

## 它解决什么问题

`PrinterStatus`（在 `app/bambu/models.py`）的字段名直接沿用拓竹报文名：
`gcode_state`、`mc_percent`（映射为 `progress`）、`subtask_name`、`layer_num`…
这对拓竹很自然，但第三方设备族完全没有这些概念：

| 概念 | 拓竹 | Klipper/Moonraker | OctoPrint |
| --- | --- | --- | --- |
| 状态机 | `gcode_state`（RUNNING/PAUSE…） | `print_stats.state`（printing/paused…） | `state.text` |
| 进度 | `mc_percent`（0-100） | `virtual_sdcard.progress`（0.0-1.0） | `progress.completion` |
| 文件名 | `subtask_name` | `print_stats.filename` | `job.file.name` |
| 层数 | `layer_num` / `total_layer_num` | `print_stats.info.*` | ❌ 没有 |
| 故障码 | `hms[]` | ❌ 没有 | ❌ 没有 |

## 设计约定

* ``DeviceStatus`` 只放**任何 3D 打印机都有的通用概念**，字段名不绑定任何厂商。
* 各设备族的适配器负责把设备私有格式**先转成通用字段字典**，再调用
  ``apply_mapped()`` 合并进来 —— 这是新增设备族的**唯一写状态入口**。
* 拓竹的 ``PrinterStatus`` **保留原有全部字段名**（老配置、老测试、老调用方
  都依赖那些字段名），它不继承本类；两者的差异由 :func:`display_status`
  在"界面这一层"抹平 —— 界面代码因此不需要知道自己在看哪一族的状态。

## 单位与取值范围（务必遵守）

* ``progress_percent``：0..100 的整数；**未知用 -1**（不是 0，0 表示"刚开始"）。
* ``remaining_minutes``：整数分钟；0 表示未知。
* ``layer_current`` / ``layer_total``：整数；0 表示未知。
* ``job_state``：只用下面 ``JOB_*`` 常量里的取值，未知用 ``JOB_UNKNOWN``。
* ``online``：遥测/控制通道是否在线。
* ``camera_online``：最近一帧是否新鲜（沿用「帧新鲜度优先」的既有判据）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

#: :func:`apply_mapped` 能接受的通用字段名。
#: 适配器传了表外的键会被忽略（并可通过 ``unknown_fields`` 观察），
#: 这样各族适配器可以放心地"多传一点"，而不用怕污染状态对象。
MAPPABLE_FIELDS = frozenset(
    {
        "online",
        "camera_online",
        "last_error",
        "job_state",
        "progress_percent",
        "remaining_minutes",
        "layer_current",
        "layer_total",
        "job_name",
        "job_error_code",
        "nozzle_temper",
        "nozzle_target_temper",
        "nozzle_temper_2",
        "nozzle_target_temper_2",
        "bed_temper",
        "bed_target_temper",
        "chamber_temper",
        "wifi_rssi_dbm",
        "speed_level",
        "light_on",
    }
)

#: 归一化后的作业状态取值。各族适配器负责把自家状态机映射到这里。
JOB_UNKNOWN = "unknown"
JOB_OFFLINE = "offline"
JOB_IDLE = "idle"
JOB_PREPARING = "preparing"
JOB_PRINTING = "printing"
JOB_PAUSED = "paused"
JOB_FINISHED = "finished"
JOB_FAILED = "failed"

#: 作业状态 -> 中文展示（界面与网页共用；第三方族也可以直接复用这套文案）
JOB_STATE_TEXT = {
    JOB_UNKNOWN: "未知",
    JOB_OFFLINE: "离线",
    JOB_IDLE: "空闲",
    JOB_PREPARING: "准备中",
    JOB_PRINTING: "打印中",
    JOB_PAUSED: "已暂停",
    JOB_FINISHED: "打印完成",
    JOB_FAILED: "打印失败",
}

#: 正在进行（未结束）的作业状态
JOB_ACTIVE_STATES = frozenset({JOB_PREPARING, JOB_PRINTING, JOB_PAUSED})

#: 视频通道状态 -> 短标签。取值与 ``DeviceSession.last_camera_state`` 一致，
#: 未知取值回落到「连接中」。
CAMERA_STATE_TEXT = {
    "connecting": "连接中",
    "streaming": "已连接",
    "retrying": "画面重连中",
    "stopped": "画面未启动",
}


def camera_status_text(
    *,
    camera_online: bool,
    mqtt_online: bool,
    camera_state: str = "",
    camera_detail: str = "",
    mqtt_auth_error: bool = False,
    has_access_code: bool = False,
    credential_label: str = "访问代码",
    credential_required: bool = True,
) -> tuple[str, str]:
    """把通道状态翻译成「短标签 + 完整说明」。

    桌面版每路画面的角标与网页版状态行要回答同一个问题——「这一路现在是什么
    情况」，以前两处各写了一份判据，而且都把完整说明截断后显示（12 / 14 个
    字符），像「RTSPS(322) 未取到画面…」被截得看不懂。

    现在统一：返回的第一项是**稳定的短标签**（宽度可控，窄处也能完整显示），
    第二项是完整说明，由调用方放到悬浮提示里。

    :param credential_label: 该设备族对凭据的叫法（拓竹「访问代码」、
        Moonraker「API Key」）。写死「访问代码」会让第三方族上出现
        「请填访问代码」而它根本没有这个概念。
    :param credential_required: 该族的凭据**是否必填**。内网 Moonraker 默认免鉴权，
        没填 API Key 是完全正常的状态 —— 以前这里会说「未配置 API Key」，
        让人以为必须去填一个本不需要的东西（真机实测就撞上了）。
    """
    detail = camera_detail or ""
    if camera_online and mqtt_online:
        return "在线", detail
    if camera_state == "auth_error" or mqtt_auth_error:
        return f"{credential_label}错误", detail
    if camera_online:
        return "画面正常·遥测断开", detail
    if credential_required and not has_access_code:
        return f"未配置{credential_label}", detail
    return CAMERA_STATE_TEXT.get(camera_state, "连接中"), detail


@dataclass
class DeviceStatus:
    """一台设备的实时状态（通用部分）。

    子类可以自由追加厂商专有字段；**合并语义必须遵守** ``apply_mapped`` 的约定：
    只覆盖本次真的提供的字段，未提供的保持原值（因为像 P1 系列这样的固件
    只推送变化过的字段）。
    """

    # --- 连接 ---
    online: bool = False
    camera_online: bool = False
    last_message_ts: float = 0.0
    last_error: str = ""
    #: 遥测通道是否在线（与 ``online`` 同义，保留是为了兼容既有调用方）
    mqtt_online: bool = False

    # --- 作业 ---
    job_state: str = JOB_UNKNOWN
    #: 0..100；-1 表示未知
    progress_percent: int = -1
    remaining_minutes: int = 0
    layer_current: int = 0
    layer_total: int = 0
    job_name: str = ""
    #: 厂商专有的单值错误码（拓竹是 print_error）
    job_error_code: int = 0

    # --- 温度 ---
    nozzle_temper: float = 0.0
    nozzle_target_temper: float = 0.0
    nozzle_temper_2: Optional[float] = None
    nozzle_target_temper_2: Optional[float] = None
    bed_temper: float = 0.0
    bed_target_temper: float = 0.0
    chamber_temper: Optional[float] = None

    # --- 其它 ---
    #: WiFi 信号强度（dBm，负值）。第三方设备族可能没有，用 None 表示未知。
    wifi_rssi_dbm: Optional[float] = None
    speed_level: Optional[int] = None
    light_on: Optional[bool] = None

    #: 原始报文（排障生命线：字段没认出来时靠它排查，**务必保留**）
    raw: dict[str, Any] = field(default_factory=dict)

    # --- 派生属性（界面与网页共用） ---
    @property
    def job_state_text(self) -> str:
        return JOB_STATE_TEXT.get(self.job_state, JOB_STATE_TEXT[JOB_UNKNOWN])

    @property
    def is_printing(self) -> bool:
        return self.job_state in JOB_ACTIVE_STATES

    @property
    def is_paused(self) -> bool:
        return self.job_state == JOB_PAUSED

    @property
    def progress_text(self) -> str:
        """进度文案；未知时给 ``--`` 而不是 0%（0% 会被误读成"刚开始"）。"""
        if self.progress_percent < 0:
            return "--"
        return f"{max(0, min(100, self.progress_percent))}%"

    @property
    def stale(self) -> bool:
        """是否长时间没有收到状态（默认 30 秒）。

        轮询型设备族（例如 Moonraker 用 2 秒轮询）如果周期不同，
        适配器应当在 ``__init__`` 里覆盖 ``stale_seconds``。
        """
        limit = getattr(self, "stale_seconds", 30.0)
        if self.last_message_ts <= 0:
            return True
        return (time.time() - self.last_message_ts) > limit

    # --- 归一化合并 ---
    def known_fields(self) -> frozenset[str]:
        """本对象里可以在 ``apply_mapped`` 中被写入的字段名。"""
        return MAPPABLE_FIELDS

    def apply_mapped(self, fields: Mapping[str, Any]) -> bool:
        """把「通用字段字典」合并进本状态。

        这是**新增设备族的唯一写状态入口**：适配器把设备私有格式转换成一个
        字段字典（键取自 :data:`MAPPABLE_FIELDS`），剩下的合并、类型转换、
        时间戳与 ``online`` 维护都由这里统一处理。

        返回是否发生了有效合并；``fields`` 里没有可识别字段时返回 False。
        """
        applied = False
        for key, value in fields.items():
            if key not in MAPPABLE_FIELDS or value is None:
                continue
            setattr(self, key, value)
            applied = True
        if applied:
            self.last_message_ts = time.time()
            self.online = True
            self.mqtt_online = True
        return applied


# ------------------------------------------------------- 界面的统一只读视图

def _wifi_level_from_dbm(dbm: Optional[float]) -> int:
    """把 RSSI(dBm) 换算成 0-4 格（阈值与拓竹那条路径保持一致）。"""
    if dbm is None:
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


def _minutes_text(minutes: int) -> str:
    """剩余时间文案：与拓竹侧 `PrinterStatus.remaining_text` 完全同款。"""
    minutes = max(0, int(minutes or 0))
    if minutes <= 0:
        return "--"
    hours, mins = divmod(minutes, 60)
    if hours:
        return f"{hours}小时{mins:02d}分"
    return f"{mins}分钟"


class DisplayStatus:
    """界面用的**只读视图**：把任一族的会话状态翻译成界面认识的那套字段名。

    ## 为什么要有这一层

    界面（`ui/tile.py`、`web/server.py`、`headless.py`）历史上只认拓竹
    ``PrinterStatus`` 的字段名（``gcode_state`` / ``progress`` / ``subtask_name``…），
    而第三方族的状态是通用的 ``DeviceStatus``（``job_state`` / ``progress_percent`` /
    ``job_name``…）。两条路可选：改全部界面代码，或者加一层翻译。

    这里选后者：**界面代码一行不用动**，翻译只有这一处，而且能单独测
    （`tests/test_display_status.py` 里对拓竹对象断言"逐字段等同原对象"，
    对通用对象断言"每个字段都有合理取值"）。

    ## 约定

    * 视图是只读的：写状态仍然只走 `DeviceStatus.apply_mapped()`；
    * 视图里的字段**始终存在**（缺失的用空值/`--` 兜底），避免界面拿到
      ``AttributeError`` —— 这是"接一个新族就崩界面"的典型原因；
    * 拓竹对象过这一层是**无损**的（同名优先取原值）。
    """

    __slots__ = ("_status",)

    def __init__(self, status: Any) -> None:
        self._status = status

    # ----------------------------------------------------------- 显式映射
    @property
    def state_text(self) -> str:
        text = getattr(self._status, "state_text", "")
        if text:
            return str(text)
        return str(getattr(self._status, "job_state_text", "") or "未知")

    @property
    def progress(self) -> int:
        """0..100。通用模型里 -1 = 未知，界面上按 0 显示（与拓竹侧行为一致）。"""
        value = getattr(self._status, "progress", None)
        if value is None:
            value = getattr(self._status, "progress_percent", 0)
        try:
            percent = int(value)
        except (TypeError, ValueError):
            return 0
        return max(0, min(100, percent))

    @property
    def remaining_text(self) -> str:
        text = getattr(self._status, "remaining_text", "")
        if text and text != "--":
            return str(text)
        return _minutes_text(int(getattr(self._status, "remaining_minutes", 0) or 0))

    @property
    def finish_time_text(self) -> str:
        text = getattr(self._status, "finish_time_text", "")
        if text:
            return str(text)
        minutes = int(getattr(self._status, "remaining_minutes", 0) or 0)
        if minutes <= 0 or not self.is_printing:
            return "--"
        from datetime import datetime, timedelta

        eta = datetime.now() + timedelta(minutes=minutes)
        if eta.date() != datetime.now().date():
            return eta.strftime("明天 %H:%M")
        return eta.strftime("%H:%M")

    @property
    def subtask_name(self) -> str:
        name = getattr(self._status, "subtask_name", "")
        if name:
            return str(name)
        return str(getattr(self._status, "job_name", "") or "")

    @property
    def layer_num(self) -> int:
        value = getattr(self._status, "layer_num", None)
        if value is None:
            value = getattr(self._status, "layer_current", 0)
        return max(0, int(value or 0))

    @property
    def total_layer_num(self) -> int:
        value = getattr(self._status, "total_layer_num", None)
        if value is None:
            value = getattr(self._status, "layer_total", 0)
        return max(0, int(value or 0))

    @property
    def gcode_state(self) -> str:
        """状态机原始取值。通用族给归一化后的 ``job_state``（界面只用它判"有没有状态"）。"""
        state = getattr(self._status, "gcode_state", "")
        if state:
            return str(state)
        return str(getattr(self._status, "job_state", "") or "")

    @property
    def ams_trays(self) -> list:
        trays = getattr(self._status, "ams_trays", None)
        return list(trays) if isinstance(trays, list) else []

    @property
    def vt_tray(self) -> Any:
        return getattr(self._status, "vt_tray", None)

    @property
    def active_tray(self) -> Any:
        return getattr(self._status, "active_tray", None)

    @property
    def hms_items(self) -> list:
        items = getattr(self._status, "hms_items", None)
        return list(items) if isinstance(items, list) else []

    @property
    def print_error(self) -> int:
        value = getattr(self._status, "print_error", None)
        if value is None:
            value = getattr(self._status, "job_error_code", 0)
        return int(value or 0)

    @property
    def print_error_text(self) -> str:
        return str(getattr(self._status, "print_error_text", "") or "")

    @property
    def lights(self) -> dict:
        lights = getattr(self._status, "lights", None)
        return dict(lights) if isinstance(lights, dict) else {}

    @property
    def wifi_signal(self) -> str:
        signal = getattr(self._status, "wifi_signal", "")
        if signal:
            return str(signal)
        dbm = getattr(self._status, "wifi_rssi_dbm", None)
        if dbm is None:
            return ""
        return f"{float(dbm):.0f}dBm"

    @property
    def wifi_level(self) -> int:
        level = getattr(self._status, "wifi_level", None)
        if isinstance(level, int) and level > 0:
            return level
        signal = str(getattr(self._status, "wifi_signal", "") or "").lower().replace("dbm", "")
        try:
            dbm: Optional[float] = float(signal.strip())
        except ValueError:
            dbm = getattr(self._status, "wifi_rssi_dbm", None)
        return _wifi_level_from_dbm(dbm)

    @property
    def light_on(self) -> Optional[bool]:
        value = getattr(self._status, "light_on", None)
        return value if isinstance(value, bool) else None

    @property
    def last_message_ts(self) -> float:
        return float(getattr(self._status, "last_message_ts", 0.0) or 0.0)

    @property
    def last_error(self) -> str:
        return str(getattr(self._status, "last_error", "") or "")

    @property
    def raw(self) -> dict:
        raw = getattr(self._status, "raw", None)
        return raw if isinstance(raw, dict) else {}

    # ----------------------------------------------------------- 兜底
    def __getattr__(self, name: str) -> Any:
        """其余成员（``nozzle_temper`` / ``mqtt_online`` / ``is_printing``…）原样透传。

        两个模型都有这些字段，没必要逐个转发；真缺失时抛 ``AttributeError``
        （而不是悄悄给个默认值），否则界面会拿着假数据继续画。
        """
        return getattr(self._status, name)


def display_status(status: Any) -> DisplayStatus:
    """把会话状态包成界面视图（已经是视图时原样返回，可反复调用）。"""
    if isinstance(status, DisplayStatus):
        return status
    return DisplayStatus(status)


# --------------------------------------------------- 多路画面（按族无关的方式取）

def cameras_of(session: Any) -> list[dict[str, Any]]:
    """这台设备有哪些画面可选。

    拓竹那族只有一路、没有 `cameras()`，返回空列表（界面据此不显示切换入口）；
    第三方族（Moonraker）给出 ``[{"index","name","location","available","url"}…]``，
    例如 Voron 的喷嘴 + 舱内两路。
    """
    getter = getattr(session, "cameras", None)
    if not callable(getter):
        return []
    try:
        items = getter()
    except Exception:  # noqa: BLE001 - 摄像头列表拿不到不该影响画面本身
        return []
    if not isinstance(items, list):
        return []
    clean: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or "index" not in item:
            continue
        try:
            index = int(item["index"])
        except (TypeError, ValueError):
            continue
        clean.append(
            {
                "index": index,
                "name": str(item.get("name", "") or f"摄像头 {index + 1}"),
                "location": str(item.get("location", "") or ""),
                "available": bool(item.get("available", True)),
                "detail": str(item.get("detail", "") or ""),
                "url": str(item.get("url", "") or ""),
                "stream_url": str(item.get("stream_url", "") or ""),
            }
        )
    return clean


def camera_frame(session: Any, index: int = 0) -> tuple[int, bytes]:
    """取某一路画面的最新帧。

    ``index = 0`` 的行为与 ``session.latest_frame()`` 完全一致：拓竹那族（以及任何
    没有多路能力的会话）根本没有这个参数，所以要容忍 ``TypeError``。
    """
    getter = getattr(session, "latest_frame", None)
    if not callable(getter):
        return 0, b""
    try:
        seq, frame = getter(int(index))
    except TypeError:
        seq, frame = getter()
    return seq or 0, frame or b""
