"""设备无关内核。

这个包里的东西**不允许**依赖任何厂商模块（`app.bambu.*`、未来的 `app.adapters.*`），
也不允许依赖 Qt。它是界面、网页、命令行共同依赖的契约层：

* ``capabilities.DeviceCapabilities`` —— 设备「能做什么」
* 本文件里的 ``DeviceSession`` —— 会话必须提供什么（协议，非基类）

## 为什么要有这一层

现在整个项目只支持拓竹，界面与网页直接依赖 `PrinterSession` 的具体实现。
要接入其它设备族（Klipper/Moonraker、OctoPrint、Snapmaker…）时，
只要新适配器满足 ``DeviceSession``，界面与网页**不需要改动**。

## 关于 ``DeviceSession`` 的形态

这里用 ``typing.Protocol``（结构化类型）而不是抽象基类：
`PrinterSession` 是既有的、被大量测试锚定的实现，不需要为了「继承」而改动它；
协议只是把「界面与网页实际依赖的成员」写成可检查的契约。
`tests/test_contracts.py` 会用 ``runtime_checkable`` 校验现有实现满足它。
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from . import registry  # noqa: F401  （对外转出，便于一处导入）
from .adapter import PollingDeviceSession  # noqa: F401  （对外转出）
from .capabilities import DeviceCapabilities  # noqa: F401  （对外转出，便于一处导入）
from .device import (
    CAMERA_STATE_TEXT,
    JOB_ACTIVE_STATES,
    JOB_FAILED,
    JOB_FINISHED,
    JOB_IDLE,
    JOB_OFFLINE,
    JOB_PAUSED,
    JOB_PREPARING,
    JOB_PRINTING,
    JOB_STATE_TEXT,
    JOB_UNKNOWN,
    MAPPABLE_FIELDS,
    DeviceStatus,
    camera_status_text,
)
from .registry import (  # noqa: F401  （对外转出）
    FAMILY_BAMBU,
    FAMILY_MOONRAKER,
    FAMILY_OCTOPRINT,
    CredentialPolicy,
    FamilyDescriptor,
)

__all__ = [
    "CAMERA_STATE_TEXT",
    "CredentialPolicy",
    "DeviceCapabilities",
    "DeviceSession",
    "DeviceSnapshot",
    "DeviceStatus",
    "FAMILY_BAMBU",
    "FAMILY_MOONRAKER",
    "FAMILY_OCTOPRINT",
    "FamilyDescriptor",
    "PollingDeviceSession",
    "JOB_ACTIVE_STATES",
    "JOB_FAILED",
    "JOB_FINISHED",
    "JOB_IDLE",
    "JOB_OFFLINE",
    "JOB_PAUSED",
    "JOB_PREPARING",
    "JOB_PRINTING",
    "JOB_STATE_TEXT",
    "JOB_UNKNOWN",
    "MAPPABLE_FIELDS",
    "camera_status_text",
    "registry",
]


class DeviceSnapshot(Protocol):
    """状态快照必须能回答的最小问题。

    界面与网页都通过 ``session.snapshot()`` 取状态，且只用到下面这些成员。
    具体的状态类（拓竹的 ``PrinterStatus``）可以远不止这些字段。
    """

    @property
    def state_text(self) -> str: ...

    @property
    def progress(self) -> int: ...

    @property
    def is_printing(self) -> bool: ...

    @property
    def is_paused(self) -> bool: ...

    @property
    def camera_online(self) -> bool: ...

    @property
    def mqtt_online(self) -> bool: ...


@runtime_checkable
class DeviceSession(Protocol):
    """一台设备监控会话的对外契约。

    成员来源：把 `ui/`、`web/`、`headless.py`、`main.py` 里对 ``session`` 的
    实际访问逐个列出来（见 `docs/ARCHITECTURE.md` 第 2 节）。
    **加成员是安全的；删或改名会让界面/网页静默失效**，因此
    `tests/test_contracts.py` 里对这套名单做了快照。
    """

    # --- 静态信息 ---
    #: 设备信息对象。至少要有 ``ip`` / ``display_name()`` / ``tile_span``。
    info: Any

    # --- 生命周期 ---
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def restart(self) -> None: ...

    # --- 状态与画面 ---
    def snapshot(self) -> Any:
        """返回当前状态快照（实现里通常是可变对象，调用方按需读取）。"""
        ...

    def latest_frame(self) -> tuple[int, Optional[bytes]]:
        """返回 ``(序号, 最新一帧 JPEG 字节)``；没有画面时 ``(0, None)``。

        用「序号 + 最新帧」而不是回调/信号，是为了让界面按自己的节奏轮询，
        高频帧不会把事件队列压爆。
        """
        ...

    @property
    def camera_fps(self) -> float: ...

    @property
    def video_backend(self) -> str:
        """当前视频通道的展示名（``"TCP6000"`` / ``"RTSPS"`` / ``"-"``）。"""
        ...

    # --- 控制 ---
    @property
    def can_control(self) -> bool:
        """现在能否下发控制命令（遥测在线、且没有其它门槛挡住）。"""
        ...

    @property
    def controls_blocked_reason(self) -> str:
        """若控制被挡住，给出可读原因；没有问题时为空字符串。

        文案要求（见 `app/bambu/printer.py` 的实现）：必须说清
        **只开局域网模式没用**、**必须先局域网再开发者**、**此时只能控灯**，
        并推荐官方「农场管家」。
        """
        ...

    @property
    def controls_blocked_short(self) -> str:
        """上面那条说明的一句话版（窄位置用，例如状态条与按钮提示）。"""
        ...

    @property
    def video_unavailable_reason(self) -> str:
        """这台设备在本机根本出不了画面时的说明（否则空串）。

        网页端会把它**直接显示在画面区域**：触屏没有悬浮提示，
        用户否则只能看到一个永远空着的画面与一句「连接中」。
        """
        ...

    def pause_print(self) -> bool: ...

    def resume_print(self) -> bool: ...

    def stop_print(self) -> bool: ...

    def set_light(self, on: bool) -> bool: ...

    def set_speed(self, level: int) -> bool: ...

    # --- 视频参数 ---
    def set_max_fps(self, fps: float) -> None: ...

    def set_video_target_size(self, width: int, height: int) -> None: ...

    # --- 诊断 ---
    def connection_summary(self) -> str: ...

    #: 运行中收集到的告警（界面「通道诊断」与自检报告会展示）
    warnings: list[str]

    #: 会话是否处于运行状态
    running: bool

    #: 最近一次视频通道状态与说明（界面用它显示「连接中 / 访问代码错误」等）
    last_camera_state: str
    last_camera_detail: str

    #: 遥测是否因凭据错误被拒（界面据此显示「访问代码错误」）
    mqtt_auth_error: bool
