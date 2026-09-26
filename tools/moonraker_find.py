"""在本地网段里找 Moonraker 设备（Klipper 机器）。

用法::

    python tools/moonraker_find.py                     # 扫所有物理网卡的 /24
    python tools/moonraker_find.py --subnet 192.168.31.0/24
    python tools/moonraker_find.py --port 7125 --port 80
    python tools/moonraker_find.py --json              # 给脚本用

## 它做什么（以及不做什么）

对网段里每个地址做一次 **TCP 连接**（不发任何数据），连上的再发一个
**只读的** ``GET /server/info`` 确认是 Moonraker，然后顺带读一次
``GET /server/webcams/list`` 看它有幾個摄像头。

**不会**下发任何控制指令、不会验证 API Key、不会尝试登录 —— 排查「找不到设备」
时用一个不改动设备的动作就够了。

拓竹的自动搜索（SSDP / 2021 广播）对 Moonraker 完全无效：那套协议是拓竹私有的，
而 Moonraker 的 mDNS 在设备端默认是关的。所以接第三方设备时，这个「按端口扫网段」
是最实在的办法（也是 `registry.py` 里列的候选发现方式之一，只是还没接进界面）。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu.discovery import local_interfaces  # noqa: E402
from app.core.registry import (  # noqa: E402
    FAMILY_MOONRAKER,
    MOONRAKER_FALLBACK_PORT,
    MOONRAKER_HTTP_PORT,
)
from tools._common import enable_utf8  # noqa: E402

DEFAULT_PORTS = (MOONRAKER_FALLBACK_PORT, MOONRAKER_HTTP_PORT)


def _local_addresses() -> set[str]:
    return {item.ip for item in local_interfaces()}


def _subnets_from_interfaces() -> list[str]:
    """物理网卡对应的网段列表（跳过虚拟网卡与回环）。

    ⚠️ 用 ``ip + mask`` 自己算网段，**不要**用 ``LocalInterface.prefix``：
    那个字段在两条代码路径上格式不一致（Windows 适配器表给 4 段的
    ``192.168.31.0``，兜底路径 ``_prefix_of()`` 给 3 段的 ``192.168.31``），
    而 3 段形式喂给 ``inet_aton`` 会被当成 ``192.168.0.31``。
    """
    found = []
    for item in local_interfaces():
        if not item.sweepable:
            continue
        mask = int(getattr(item, "mask", 24) or 24)
        network = ipaddress.ip_network(f"{item.ip}/{mask}", strict=False)
        found.append(str(network))
    return found


def _hosts(subnet: str, skip: set[str]) -> list[str]:
    network = ipaddress.ip_network(subnet, strict=False)
    return [str(host) for host in network.hosts() if str(host) not in skip]


def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _get_json(host: str, port: int, path: str, timeout: float) -> dict | None:
    """只读 GET；失败返回 None（不抛异常，扫描时失败是常态）。"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def probe(host: str, ports: tuple[int, ...], timeout: float) -> dict | None:
    """探测一个地址：哪个端口是 Moonraker，返回它的基本信息。"""
    for port in ports:
        if not _port_open(host, port, timeout):
            continue
        info = _get_json(host, port, "/server/info", timeout)
        if not isinstance(info, dict) or "result" not in info:
            continue
        result = info.get("result") or {}
        webcams = _get_json(host, port, "/server/webcams/list", timeout) or {}
        cams = (webcams.get("result") or {}).get("webcams") or []
        names = []
        for cam in cams:
            if isinstance(cam, dict):
                names.append(
                    {
                        "name": str(cam.get("name", "")),
                        "snapshot": str(cam.get("snapshot_url", "")),
                        "stream": str(cam.get("stream_url", "")),
                        "enabled": bool(cam.get("enabled", True)),
                    }
                )
        return {
            "ip": host,
            "port": port,
            "moonraker": str(result.get("moonraker_version", "")),
            "klipper": str(result.get("klipper_version", "")),
            "klippy_state": str(result.get("klippy_state", "")),
            "webcams": names,
        }
    return None


def main(argv: list[str] | None = None) -> int:
    enable_utf8()
    parser = argparse.ArgumentParser(description="扫描本地网段里的 Moonraker（只读）")
    parser.add_argument("--subnet", action="append", default=[], help="手工指定网段，可重复")
    parser.add_argument("--port", action="append", type=int, default=[], help="端口，可重复")
    parser.add_argument("--timeout", type=float, default=0.35, help="单个地址的连接超时（秒）")
    parser.add_argument("--workers", type=int, default=64, help="并发数")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    ports = tuple(args.port) or DEFAULT_PORTS
    subnets = args.subnet or _subnets_from_interfaces()
    if not subnets:
        print("没有找到可扫描的物理网卡，请用 --subnet 手工指定（例如 192.168.31.0/24）")
        return 2

    skip = _local_addresses()
    targets: list[str] = []
    for subnet in subnets:
        try:
            targets.extend(_hosts(subnet, skip))
        except ValueError:
            print(f"网段写法不对：{subnet}")
            return 2

    print(f"扫描 {len(subnets)} 个网段（{'、'.join(subnets)}），端口 {list(ports)}，"
          f"共 {len(targets)} 个地址…")
    started = time.time()
    found: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(4, args.workers)) as pool:
        futures = {pool.submit(probe, host, ports, args.timeout): host for host in targets}
        for future in futures:
            try:
                device = future.result()
            except Exception:  # noqa: BLE001 - 单个地址探测失败不影响整轮扫描
                continue
            if device:
                found.append(device)
                print(f"  找到 {device['ip']}:{device['port']}  "
                      f"Moonraker {device['moonraker'] or '?'} / Klipper {device['klipper'] or '?'}  "
                      f"klippy={device['klippy_state'] or '?'}  摄像头 {len(device['webcams'])} 个")
    elapsed = time.time() - started

    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2))
    elif not found:
        print(f"\n{elapsed:.1f} 秒内没有找到 Moonraker 设备。")
        print("可能的原因：设备不在这个网段、Moonraker 换了端口（用 --port 指定），"
              "或者防火墙挡了。也可以直接问设备主人要 IP。")
        return 1
    else:
        print(f"\n{elapsed:.1f} 秒扫描完成，找到 {len(found)} 台：")
        for device in found:
            base = f"http://{device['ip']}:{device['port']}"
            print(f"\n{device['ip']}:{device['port']}")
            print(f"  Moonraker {device['moonraker'] or '?'}，Klipper {device['klipper'] or '?'}，"
                  f"klippy={device['klippy_state'] or '?'}")
            if not device["webcams"]:
                print("  摄像头：Moonraker 里没有配置（/server/webcams/list 是空的）——"
                      "需要看 crowsnest 的配置，或者摄像头不走 Moonraker 管理")
            for cam in device["webcams"]:
                state = "" if cam["enabled"] else "（已禁用）"
                print(f"  摄像头 {cam['name']}{state}")
                print(f"    快照 {cam['snapshot'] or '-'}")
                print(f"    流   {cam['stream'] or '-'}")
            print(f"  可以这样加进程序：python -m app.headless --add-printer "
                  f"\"moonraker@{device['ip']} {device['ip']} {device['port']}\"")
            print(f"  或者先看它还有什么能力：{base}/printer/objects/list")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
