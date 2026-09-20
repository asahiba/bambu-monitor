"""Klipper / Moonraker 生态适配器。"""

from typing import Any

from .adapter import QUERY_OBJECTS, STATE_MAP, MoonrakerAdapter, parse_status

__all__ = ["MoonrakerAdapter", "QUERY_OBJECTS", "STATE_MAP", "create_session", "parse_status"]


def create_session(info: Any, **options: Any) -> MoonrakerAdapter:
    """Moonraker 族的会话工厂（由 `app/core/registry.py` 登记）。

    端口与凭据都从 ``PrinterInfo`` 里取，因此"配置里怎么填"和"怎么连"只有一处：

    * ``info.port`` 为空（0）时用族的默认端口 ``80``（U1 出厂配置前面挂了 nginx，
      80 端口即可访问 Moonraker API；通用 Klipper 机器填 7125）；
    * ``info.api_key`` 是可选凭据（内网默认免鉴权），非标准网段才需要。

    调用方仍可用 ``options`` 覆盖（桌面版"测试连接"就会显式传 ``base_url``）：
    同名参数优先于配置里的值。
    """
    from ...core.registry import default_port

    port = default_port(info)
    host = str(getattr(info, "ip", "") or "")
    base_url = str(options.pop("base_url", "") or "") or f"http://{host}:{port}"
    api_key = str(options.pop("api_key", "") or "") or str(getattr(info, "api_key", "") or "")
    camera_url = str(options.pop("camera_url", "") or "") or str(
        getattr(info, "camera_url", "") or ""
    )
    return MoonrakerAdapter(
        info,
        base_url=base_url,
        api_key=api_key,
        camera_url=camera_url,
        **options,
    )
