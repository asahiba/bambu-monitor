"""开发用：连续跑多次自动搜索，统计发现数量与稳定性。

用法：``python tools/discovery_bench.py [次数] [每次秒数]``
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu.discovery import DiscoveryService, local_interfaces  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

enable_utf8()

runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0

print("本机网卡：")
for iface in local_interfaces():
    print(f"  {iface.ip:16} 广播 {iface.broadcast:16} {'（跳过扫描）' if not iface.scannable else ''}")

union: dict[str, str] = {}
counts: list[int] = []
for index in range(runs):
    print(f"\n===== 第 {index + 1}/{runs} 次搜索（{timeout:.0f} 秒）=====")
    service = DiscoveryService(on_found=lambda info: None, timeout=timeout)
    started = time.time()
    found = service.run_blocking()
    elapsed = time.time() - started
    counts.append(len(found))
    for info in found:
        key = info.serial or info.ip
        union[key] = info.ip
        print(f"  {info.ip:16} {info.model.label:10} {info.serial:18} {info.name}")
    print(f"  本次发现 {len(found)} 台，用时 {elapsed:.1f}s，发送 {service.packets_sent} 个搜索包")
    by_source: dict[str, int] = {}
    for sources in service.hit_sources.values():
        for source in sources:
            by_source[source] = by_source.get(source, 0) + 1
    print(f"  通道命中统计：{by_source}")

print("\n" + "=" * 70)
print(f"各次数量：{counts}（最多 {max(counts)}，最少 {min(counts)}）")
print(f"并集共 {len(union)} 台：{sorted(union.values())}")
if counts and min(counts) == max(counts) == len(union):
    print("结论：每次都能搜到全部设备 ✓")
else:
    print("结论：仍存在漏搜 ✗（看并集与各次的差值）")
