"""开发用：连续多次枚举本机适配器表，检查网卡枚举是否稳定。"""

from __future__ import annotations

import ctypes
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu import discovery as disc  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

enable_utf8()

print("=== 直接读适配器表（不过滤，看真实状态）===")
for round_index in range(5):
    iphlpapi = ctypes.windll.iphlpapi
    size = ctypes.c_ulong(16 * 1024)
    buffer = ctypes.create_string_buffer(size.value)
    flags = 0x02 | 0x04 | 0x08
    result = iphlpapi.GetAdaptersAddresses(
        ctypes.c_ulong(2),
        ctypes.c_ulong(flags),
        None,
        ctypes.cast(buffer, ctypes.POINTER(disc._AdapterAddresses)),
        ctypes.byref(size),
    )
    rows = []
    adapter = ctypes.cast(buffer, ctypes.POINTER(disc._AdapterAddresses))
    while adapter:
        node = adapter.contents
        ips = []
        unicast = node.FirstUnicastAddress
        while unicast:
            entry = unicast.contents
            if entry.Address.lpSockaddr:
                sa = ctypes.cast(entry.Address.lpSockaddr, ctypes.POINTER(disc._SockaddrIn)).contents
                if sa.sin_family == 2:
                    ips.append(
                        f"{socket.inet_ntoa(bytes(bytearray(sa.sin_addr)))}/{entry.OnLinkPrefixLength}"
                    )
            unicast = entry.Next
        rows.append(f"{node.FriendlyName or '?':32} type={node.IfType} status={node.OperStatus} {ips}")
        adapter = node.Next
    print(f"\n第 {round_index + 1} 次：ret={result} 适配器 {len(rows)} 个")
    for row in rows:
        print("   ", row)
    print("   local_interfaces() ->", [i.ip for i in disc.local_interfaces()])
    time.sleep(0.5)
