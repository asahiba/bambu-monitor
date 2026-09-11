"""设备能力描述：让上层按「能力」而不是「机型/品牌」分支。

## 为什么需要它

改造前，界面与网页里到处是这类判断（同一段表达式在三处重复）：

* 「这台机器有没有腔温传感器」→ 决定要不要显示仓温
  （`ui/tile.py`、`web/server.py`、`main.py` 各写了一遍 `info.model.has_chamber_sensor`）
* 「走 6000 端口还是 RTSPS」→ 决定视频通道
* 「有没有 AMS」→ 决定要不要显示耗材行

其中一部分是**机型固有属性**（腔温、RTSPS），一部分是**运行时才知道的**
（是否挂了 AMS、当前是单喷嘴还是双喷嘴）。把它们统一成 ``DeviceCapabilities``
之后，界面只问能力、不问品牌，接入第三方设备族（Klipper/Moonraker、
OctoPrint、Snapmaker 等）时 UI 与网页**不需要任何改动**。

## 与 PrinterStatus 的分工

* ``DeviceCapabilities`` = **能不能**（有没有这个功能）
* ``PrinterStatus`` = **是多少**（当前的读数）

例如 ``capabilities.has_chamber_sensor`` 表示「这机器有腔温探头」，
而 ``status.chamber_temper`` 是当前温度值；两者都为真才显示仓温。

## 层次

``DeviceCapabilities`` 放在 `app/core/` 而不是 `app/bambu/`：它是**设备无关**的契约，
第三方适配器（`app/adapters/*`）与界面都依赖它，不应该让它们反向依赖拓竹协议包。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

#: 视频通道标识。
#:
#: ⚠️ 这三个取值源自拓竹的协议名（6000 端口 / RTSPS 322 / 自动），是**拓竹族**的
#: 通道命名。接入第三方设备族时不要往这里塞新通道名，而应当在适配器里把各自的通道
#: （MJPEG、HTTP 快照、WebRTC…）映射到自己的 ``video_channel`` 值域；
#: 界面只把它当字符串展示（``video_backend``）。
VIDEO_TCP6000 = "tcp6000"
VIDEO_RTSP = "rtsp"
VIDEO_AUTO = "auto"


@dataclass(frozen=True)
class DeviceCapabilities:
    """一台设备「能做什么」。不可变，便于安全地在多线程间共享与比较。"""

    # --- 视频 ---
    #: 可用的视频通道："tcp6000" / "rtsp" / "auto"（auto = 先试 RTSPS 再退 6000）
    video_channel: str = VIDEO_TCP6000
    #: 是否有摄像头（未来支持无摄像头的设备族时需要）
    has_camera: bool = True

    # --- 传感器 ---
    #: 是否有腔温探头（A1/P1 固件会报无效值，必须不显示）
    has_chamber_sensor: bool = False
    #: 喷嘴数量（多喷嘴机型会显示第二路温度）
    nozzle_count: int = 1
    #: 是否有 WiFi 信号强度上报
    has_wifi_signal: bool = True

    # --- 耗材 ---
    #: 是否可能挂 AMS（真机上有无 AMS 要看 status.ams_exist）
    supports_ams: bool = True
    #: 是否支持外挂料盘
    supports_external_spool: bool = True

    # --- 控制 ---
    #: 支持暂停/继续
    can_pause: bool = True
    #: 支持停止
    can_stop: bool = True
    #: 支持开关舱灯（A1 系列等没有舱灯）
    can_control_light: bool = True
    #: 支持速度档位
    can_set_speed: bool = True

    # --- 诊断 ---
    #: 是否有 HMS/错误码体系（第三方设备族一般没有）
    has_hms: bool = True

    def merged(self, **changes: object) -> "DeviceCapabilities":
        """派生一份修改后的能力（用于叠加运行时观测到的信息）。"""
        return replace(self, **changes)  # type: ignore[arg-type]

    @property
    def is_rtsp_only(self) -> bool:
        """是否只能走 RTSPS（这类机型回退到 6000 必然失败，绝不能退）。"""
        return self.video_channel == VIDEO_RTSP
