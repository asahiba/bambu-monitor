"""开发用：验证「无界面服务模式」（Linux / Docker 用的内核）。

关键验证点：**在完全无法导入 PySide6 的情况下**也能跑起来
（模拟 Qt 不存在的 Linux 容器环境），并检查网页服务与 PWA 资源。

用法：``python tools/headless_check.py [端口]``
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools._common import enable_utf8  # noqa: E402

enable_utf8()

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18099
CONFIG_DIR = tempfile.mkdtemp(prefix="bambu-headless-check-")
os.environ["BAMBU_MONITOR_CONFIG_DIR"] = CONFIG_DIR

# 模拟「没有 Qt 的 Linux 环境」：让任何 PySide6 导入都失败
sys.modules["PySide6"] = None  # type: ignore[assignment]

from app.config import AppConfig  # noqa: E402
from app.headless import run_headless  # noqa: E402

ok = True
base = f"http://127.0.0.1:{PORT}"
stop = threading.Event()
server_failed: list[str] = []


def serve() -> None:
    try:
        run_headless(
            [
                "--sim", "4",
                "--port", str(PORT),
                "--sim-code", "12345678",
                "--status-interval", "0",
            ]
        )
    except Exception as exc:  # noqa: BLE001
        server_failed.append(f"{exc.__class__.__name__}: {exc}")
        print(f"✗ 无界面模式启动失败：{server_failed[-1]}")
    finally:
        stop.set()


print("① 在「无法导入 PySide6」的环境下启动无界面服务（4 台模拟打印机）")
thread = threading.Thread(target=serve, daemon=True)
thread.start()

deadline = time.time() + 30
while time.time() < deadline:
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=2) as response:
            if response.status == 200:
                break
    except Exception:  # noqa: BLE001
        time.sleep(0.5)
else:
    print("   ✗ 服务未在 30 秒内就绪")
    sys.exit(1)
print(f"   服务已就绪：{base}")
if server_failed:
    ok = False

token = AppConfig.load().web_token
print(f"   配置文件：{os.path.join(CONFIG_DIR, 'config.json')}（令牌 {len(token)} 位）")

print("\n② 静态资源与 PWA 文件")
checks = [
    ("/", "text/html", b'rel="manifest"'),
    ("/manifest.webmanifest", "application/manifest", b'"display"'),
    ("/sw.js", "text/javascript", b"serviceWorker" if False else b"addEventListener"),
    ("/icon-192.png", "image/png", b"\x89PNG"),
    ("/icon-512.png", "image/png", b"\x89PNG"),
    ("/favicon.ico", "image/png", b"\x89PNG"),
]
for path, expect_type, needle in checks:
    try:
        with urllib.request.urlopen(f"{base}{path}?token={token}", timeout=5) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
            good = expect_type in content_type and needle in body
            print(f"   {path:24} {response.status} {content_type.split(';')[0]:28} "
                  f"{len(body):>6} 字节 {'✓' if good else '✗'}")
            if not good:
                ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"   {path:24} ✗ {exc}")
        ok = False

print("\n③ 状态接口")
try:
    with urllib.request.urlopen(f"{base}/api/printers?token={token}", timeout=8) as response:
        data = json.loads(response.read().decode("utf-8"))
    print(f"   共 {data['total']} 台 · 画面在线 {data['camera_online']} · 遥测在线 {data['mqtt_online']}")
    if data["total"] != 4 or data["camera_online"] < 4:
        print("   ✗ 模拟打印机数量或在线数不对")
        ok = False
    else:
        print("   ✓ 4 台模拟打印机全部有画面（Qt 缺失时自动用 OpenCV 绘制）")
except Exception as exc:  # noqa: BLE001
    print(f"   ✗ {exc}")
    ok = False

print("\n④ 单连接多路复用")
try:
    frames: dict[int, int] = {}
    with urllib.request.urlopen(f"{base}/api/live?token={token}", timeout=10) as response:
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
                kind, index = buffer[2], buffer[3] | (buffer[4] << 8)
                length = int.from_bytes(buffer[5:9], "little")
                if len(buffer) < 9 + length:
                    break
                if kind == 1:
                    frames[index] = frames.get(index, 0) + 1
                del buffer[: 9 + length]
    print(f"   收到画面的路数：{len(frames)} → {sorted(frames.items())}")
    if len(frames) < 4:
        ok = False
        print("   ✗ 多路复用通道未推满 4 路")
except Exception as exc:  # noqa: BLE001
    print(f"   ✗ {exc}")
    ok = False

print("\n⑤ 确认全程未使用 Qt")
print(f"   PySide6 在本次进程中：{'被屏蔽（模拟无 Qt 环境）' if sys.modules.get('PySide6') is None else '可用'}")

print("\n" + "=" * 62)
print("无界面模式检查：" + ("全部通过 ✓" if ok else "存在失败项 ✗"))
print("=" * 62)
sys.exit(0 if ok else 1)
