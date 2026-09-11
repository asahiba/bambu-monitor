"""设备族注册表：把「新增一个品牌/生态」变成「注册一个描述符」。

## 为什么需要它

没有注册表时，"支持新品牌"意味着在代码里到处加 `if family == ...` 分支：
界面要按品牌决定凭据字段叫什么、命令行要按品牌决定参数怎么给、
发现流程要按品牌决定用哪个探测器。有了注册表，这些差异都收敛成一条数据。

## 一个设备族描述符包含什么

* ``family``：稳定 id（会写进配置文件，**不要随意改名**）
* ``label``：界面展示名
* ``credential``：凭据策略（字段叫什么、是否必填、怎么填）
* ``default_port``：该族的主要服务端口（用于手动添加与端口探测）
* ``session_factory``：如何为一个 ``DeviceInfo`` 建会话
* ``discoveries``：发现方式说明（供界面提示与诊断展示）
* ``capabilities_summary``：该族通常具备的能力（供"添加打印机"表单做默认值）

## 拓竹族为什么也放进来

现有配置里的 ``PrinterInfo`` 没有 ``family`` 字段，**历史上写出的所有配置文件
都隐含是拓竹**。``resolve_family()`` 因此把"没有 family"一律解析为 ``bambu``，
这就是向后兼容的落点：老配置不需要迁移即可继续用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

#: 拓竹族的 id（配置文件兼容性的一部分，**不要改**）
FAMILY_BAMBU = "bambu"
#: Klipper / Moonraker 生态（覆盖 Voron、RatOS、刷 Klipper 的 Creality/Elegoo/Anycubic、
#: Snapmaker U1 等）
FAMILY_MOONRAKER = "moonraker"
#: OctoPrint 生态
FAMILY_OCTOPRINT = "octoprint"

#: Moonraker 的候选端口。
#: 官方 moonraker.conf 默认 ``port: 7125``，但 Snapmaker U1 出厂配置前面挂了
#: nginx 反代，**80 端口也能直接访问 Moonraker API**（官方端口表与真机探测都确认）。
#: 因此顺序是「80 优先、7125 回退」——U1 上 80 一定通，通用 Klipper 机器上 7125 通。
MOONRAKER_HTTP_PORT = 80
MOONRAKER_FALLBACK_PORT = 7125


@dataclass(frozen=True)
class CredentialPolicy:
    """一个设备族需要用户填什么凭据。

    界面据此决定表单里那行的标签、提示与是否必填；命令行据此决定参数校验。
    以前这些文案是写死的「访问代码」，对 Moonraker（无凭据或 API Key）
    与 OctoPrint（API Key）都不适用。
    """

    #: 配置文件里的字段名（保持 `access_code` 不变以兼容老配置）
    key: str = "access_code"
    #: 给用户看的标签
    label: str = "访问代码"
    #: 是否必填才能连上（Moonraker 内网默认免鉴权 → False）
    required: bool = True
    #: 输入框提示
    hint: str = "打印机屏幕 → 设置 → 网络 → 局域网访问代码（8 位）"
    #: 是否当作口令隐藏显示
    secret: bool = True


@dataclass(frozen=True)
class FamilyDescriptor:
    """一个设备族的全部差异。"""

    family: str
    label: str
    credential: CredentialPolicy
    #: 该族的主要服务端口（手动添加时的默认值、诊断时的探测目标）
    default_port: int = 0
    #: 候选端口（按顺序尝试）。第三方设备族常见"80 优先、7125 回退"这类形态。
    candidate_ports: tuple[int, ...] = ()
    #: 该族支持的发现方式说明（界面提示 + 诊断展示）
    discoveries: tuple[str, ...] = ()
    #: 建会话的工厂：``(info, **options) -> DeviceSession``。
    #: 允许为 None（例如只有探测能力、还不能监控）。
    session_factory: Optional[Callable[..., Any]] = None
    #: 该族是否由本程序内置（False 表示由插件/外部注册）
    builtin: bool = True
    #: 备注：给维护者看的注意事项（例如安全红线）
    notes: str = ""


_REGISTRY: dict[str, FamilyDescriptor] = {}


def register(descriptor: FamilyDescriptor) -> FamilyDescriptor:
    """注册（或覆盖）一个设备族描述符。返回该描述符便于链式使用。"""
    _REGISTRY[descriptor.family] = descriptor
    return descriptor


def get(family: str) -> Optional[FamilyDescriptor]:
    return _REGISTRY.get(family)


def all_families() -> list[FamilyDescriptor]:
    """全部已注册的设备族（按 id 排序，保证界面展示顺序稳定）。"""
    return [_REGISTRY[key] for key in sorted(_REGISTRY)]


def resolve_family(info: Any) -> FamilyDescriptor:
    """解析一台设备属于哪个族。

    **向后兼容的关键**：老配置里的 ``PrinterInfo`` 没有 ``family`` 字段，
    一律解析为拓竹族 —— 这样升级后老用户的配置不需要任何迁移。
    """
    family = (getattr(info, "family", "") or "").strip()
    if family and family in _REGISTRY:
        return _REGISTRY[family]
    # 不认识的 family 值（例如配置来自更新的版本）也退回拓竹，
    # 而不是让界面拿到 None 崩掉
    return _REGISTRY[FAMILY_BAMBU]


def is_registered(family: str) -> bool:
    return family in _REGISTRY


def _register_builtins() -> None:
    """登记内置设备族。

    只登记**已经能用**的族：提前登记会让界面出现一个选了也没用的选项，比没有更糟。
    """
    from ..bambu.ports import CAMERA_PORT, MQTT_PORT, RTSP_PORT

    register(
        FamilyDescriptor(
            family=FAMILY_BAMBU,
            label="拓竹 (Bambu Lab)",
            credential=CredentialPolicy(
                key="access_code",
                label="访问代码",
                required=True,
                hint="打印机屏幕 → 设置 → 网络 → 局域网访问代码（8 位）",
                secret=True,
            ),
            default_port=MQTT_PORT,
            candidate_ports=(MQTT_PORT, CAMERA_PORT, RTSP_PORT),
            discoveries=(
                "SSDP 组播 239.255.255.250:1990",
                "旧版 JSON 广播 255.255.255.255:2021（M99999）",
                "网段单播扫描",
                "手动填写 IP",
            ),
            notes=(
                "新机型（H2C/H2S/X2D/P2S/A2L）的 fun 字段会要求 MQTT 命令签名，"
                "未开 Developer Mode 时控制会被静默忽略（见 docs/FIELD_NOTES.md）"
            ),
        )
    )

    register(
        FamilyDescriptor(
            family=FAMILY_MOONRAKER,
            label="Klipper / Moonraker（含 Snapmaker U1）",
            credential=CredentialPolicy(
                key="api_key",
                label="API Key",
                # 内网默认免鉴权（trusted_clients 含各私网段），因此不是必填；
                # 非标准网段或开了 force_logins 时会 401，那时才需要填。
                required=False,
                hint="一般留空即可；若提示未授权，从 Moonraker 的 /access/api_key 获取",
                secret=True,
            ),
            default_port=MOONRAKER_FALLBACK_PORT,
            # 官方与真机探测都确认 80 可访问（nginx 反代到 Moonraker），7125 为回退
            candidate_ports=(MOONRAKER_HTTP_PORT, MOONRAKER_FALLBACK_PORT),
            discoveries=(
                "mDNS _moonraker._tcp.local.（需设备端开启 zeroconf，默认不开）",
                "mDNS _snapmaker._tcp.local.（Snapmaker U1 的主通道）",
                "端口探测 80 / 7125",
                "手动填写 IP",
            ),
            notes=(
                "急停与 /printer/control/* 是 WebSocket-only，HTTP 发不通，"
                "必须常驻一条 WS 连接；U1 的摄像头也靠该连接周期性保活，"
                "不保活画面就静止。详见 docs/FIELD_NOTES.md"
            ),
        )
    )


_register_builtins()
