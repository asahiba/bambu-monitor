"""开发用：对**正在运行**的软件（或 Docker 容器）做一次网页服务检查。

用法：``python tools/web_live_check.py [端口] [访问令牌]``
不带令牌时从本机配置里读取。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import AppConfig  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

enable_utf8()

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
token = sys.argv[2] if len(sys.argv) > 2 else AppConfig.load().web_token
base = f"http://127.0.0.1:{port}"
print(f"目标 {base}（令牌 {len(token)} 位）")

ok = True
try:
    with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
        print(f"① /health -> {response.status} {response.read().decode()}")
except Exception as exc:  # noqa: BLE001
    print(f"✗ /health 失败：{exc}")
    sys.exit(1)

try:
    urllib.request.urlopen(f"{base}/api/printers", timeout=5)
    print("② 无令牌访问：未被拒绝 ✗（应当 401）")
    ok = False
except urllib.error.HTTPError as exc:
    print(f"② 无令牌访问 -> {exc.code} ✓")

try:
    with urllib.request.urlopen(f"{base}/api/printers?token={token}", timeout=8) as response:
        data = json.loads(response.read().decode("utf-8"))
    print(
        f"③ /api/printers -> 共 {data['total']} 台 · 画面在线 {data['camera_online']} · "
        f"遥测在线 {data['mqtt_online']} · 打印中 {data['printing']} · 网页帧率 {data['web_fps']}"
    )
    for item in data["printers"][:14]:
        if item["camera_online"] or item["mqtt_online"]:
            print(
                f"   · {item['name']:12} {item['state_text']:6} {item['progress']:>3}% "
                f"喷嘴 {item['nozzle']}/{item['nozzle_target']}℃ 热床 {item['bed']}/{item['bed_target']}℃ "
                f"{item['backend']:8} {item['fps']}fps 跨度 {item['span']}"
            )
except Exception as exc:  # noqa: BLE001
    print(f"✗ /api/printers 失败：{exc}")
    ok = False

# 找一路在线的画面，检查 MJPEG 推流
online = [item["index"] for item in data["printers"] if item["camera_online"]] if ok else []
if online:
    index = online[0]
    print(f"④ MJPEG /stream/{index}（读取 3 秒）")
    try:
        with urllib.request.urlopen(f"{base}/stream/{index}?token={token}", timeout=8) as response:
            payload = bytearray()
            started = time.time()
            while time.time() - started < 3:
                block = response.read(8192)
                if not block:
                    break
                payload += block
        frames = payload.count(b"--bmframe")
        print(f"   {len(payload) // 1024} KB，{frames} 帧（约 {frames / 3:.1f} fps）")
        if frames < 1:
            ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"   ✗ 推流失败：{exc}")
        ok = False
else:
    print("④ 跳过 MJPEG 检查（当前没有在线画面）")

print("\n④ 单连接多路复用 /api/live（读取 4 秒）")
try:
    request = urllib.request.Request(f"{base}/api/live?token={token}")
    frames: dict[int, int] = {}
    status_records = 0
    with urllib.request.urlopen(request, timeout=10) as response:
        buffer = bytearray()
        started = time.time()
        while time.time() - started < 4:
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
                if kind == 1:
                    frames[index] = frames.get(index, 0) + 1
                elif kind == 2:
                    status_records += 1
                del buffer[: 9 + length]
    print(f"   有画面的路数：{len(frames)} → {sorted(frames.items())}")
    print(f"   状态记录：{status_records} 条")
    if not frames:
        ok = False
        print("   ✗ 多路复用通道没有推送画面")
except Exception as exc:  # noqa: BLE001
    print(f"   ✗ /api/live 失败：{exc}")
    ok = False

print("\n④ PWA 资源（手机「添加到主屏幕」用）")
for path, needle in (
    ("/manifest.webmanifest", b'"display"'),
    ("/icon-192.png", b"\x89PNG"),
    ("/icon-512.png", b"\x89PNG"),
    ("/sw.js", b"addEventListener"),
    ("/favicon.ico", b"\x89PNG"),
):
    try:
        with urllib.request.urlopen(f"{base}{path}?token={token}", timeout=5) as response:
            body = response.read()
        good = needle in body
        print(f"   {path:22} {response.status} {len(body):>6} 字节 {'✓' if good else '✗'}")
        ok = ok and good
    except Exception as exc:  # noqa: BLE001
        print(f"   {path:22} ✗ {exc}")
        ok = False

print("\n网页服务检查：" + ("通过 ✓" if ok else "存在问题 ✗"))
sys.exit(0 if ok else 1)
