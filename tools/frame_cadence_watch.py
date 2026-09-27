"""开发用：量**正在运行**的实例里每台设备的真实出帧间隔（只读）。

用法：``python tools/frame_cadence_watch.py [端口] [秒数]``

## 为什么需要它

「画面还在不在」的判据以前是写死一个秒数（6 秒），但 6000 端口的 A1 / P1 系列
根本不是每秒出图 —— 实测间隔中位数 2.8~8.8 秒、最大 12~21 秒，十几秒没有新帧
是常态（见 `docs/FIELD_NOTES.md` §2.3）。**结论是从这个工具量出来的**，
所以它留在仓库里：下次怀疑「画面在闪」或被判离线时，先量一遍。

原理：连本机网页服务的 `/api/live`，它只在帧序号变化时推一帧，
所以「收到帧的时刻」就是这台设备真正出图的时刻。全程只读：
不连打印机、不写配置、不发任何控制命令。
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import AppConfig  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

enable_utf8()

config = AppConfig.load()
port = int(sys.argv[1]) if len(sys.argv) > 1 else int(config.web_port or 8095)
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 90.0
base = f"http://127.0.0.1:{port}"
token = config.web_token

names: dict[int, str] = {}
#: (设备序号) -> 每一帧到达的时刻
arrivals: dict[int, list[float]] = {}

request = urllib.request.Request(f"{base}/api/live?token={token}")
started = time.time()
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        buffer = bytearray()
        while time.time() - started < seconds:
            block = response.read(16384)
            if not block:
                break
            buffer += block
            while True:
                position = buffer.find(b"BM")
                if position < 0 or len(buffer) < position + 9:
                    break
                if position:
                    del buffer[:position]
                kind = buffer[2]
                index = buffer[3] | (buffer[4] << 8)
                length = int.from_bytes(buffer[5:9], "little")
                if len(buffer) < 9 + length:
                    break
                payload = bytes(buffer[9 : 9 + length])
                del buffer[: 9 + length]
                if kind == 1:
                    arrivals.setdefault(index, []).append(time.time())
                elif kind == 2:
                    try:
                        data = json.loads(payload.decode("utf-8"))
                    except ValueError:
                        continue
                    for item in data.get("printers", []):
                        names[item["index"]] = item["name"]
except Exception as exc:  # noqa: BLE001
    print(f"✗ 连不上正在运行的网页服务（{base}）：{exc}")
    print("  请先启动程序（桌面版直接开着即可，无界面版加 --web-port 指定端口）")
    sys.exit(1)

print(f"采样 {round(time.time() - started, 1)} 秒（网页按帧序号变化推流，量化 1 秒级）\n")
print(f"{'#':>3} {'设备':16}{'帧数':>5}{'间隔中位':>10}{'间隔最大':>10}{'>6秒':>6}{'等效fps':>9}")
for index, stamps in sorted(arrivals.items()):
    name = names.get(index, "")[:14]
    if len(stamps) < 3:
        print(f"{index:>3} {name:16}{len(stamps):>5}      帧太少，跳过")
        continue
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    span = stamps[-1] - stamps[0]
    print(
        f"{index:>3} {name:16}{len(stamps):>5}{statistics.median(gaps):>10.1f}"
        f"{max(gaps):>10.1f}{sum(1 for g in gaps if g > 6.0):>6}"
        f"{(len(stamps) / span if span else 0):>9.2f}"
    )
