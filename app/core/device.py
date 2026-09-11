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
* 拓竹的 ``PrinterStatus`` 继承本类并保留原有全部字段（老配置、老测试、老调用方
  都依赖那些字段名），只额外提供指向通用名的别名属性。

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
