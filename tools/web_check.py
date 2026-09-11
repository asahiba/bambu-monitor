"""开发用：端到端验证网页监控服务（令牌鉴权 / JSON API / MJPEG 推流）。

用法：``python tools/web_check.py [端口]``
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu.models import PrinterInfo  # noqa: E402
from app.bambu.printer import PrinterSession  # noqa: E402
from app.sim.simulator import start_simulator, stop_simulator  # noqa: E402
from app.web.server import WebServer  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

enable_utf8()

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
TOKEN = "testtoken123"
CODE = "12345678"

print("启动模拟打印机（8 台，用于验证超过 6 路的情况）…")
printers, responder = start_simulator(8, CODE)
sessions = [
    PrinterSession(
        PrinterInfo(
            ip=printer.ip,
            serial=printer.serial,
            name=printer.name,
            access_code=CODE,
            model=__import__("app.bambu.models", fromlist=["detect_model"]).detect_model(
                printer.serial, printer.model
            ),
            stream_mode="tcp6000",
        )
    )
    for printer in printers
]
for session in sessions:
    session.start()

server = WebServer(lambda: list(sessions), port=PORT, token=TOKEN, fps=4.0, max_width=720)
if not server.start():
    print(f"✗ 端口 {PORT} 无法监听")
    stop_simulator(printers, responder)
    sys.exit(2)
print(f"网页服务已启动：{server.primary_url()}")

base = f"http://127.0.0.1:{PORT}"
ok = True
try:
    # 等待画面就绪
    deadline = time.time() + 15
    while time.time() < deadline:
        if all(session.latest_frame()[1] for session in sessions):
            break
        time.sleep(0.5)

    print("\n① 无令牌访问 /api/printers")
    try:
        with urllib.request.urlopen(f"{base}/api/printers", timeout=5) as response:
            print(f"   状态码 {response.status}（期望 401）")
            ok = False
    except urllib.error.HTTPError as exc:
        print(f"   状态码 {exc.code} ✓")

    print("\n② 带令牌访问 /api/printers")
    with urllib.request.urlopen(f"{base}/api/printers?token={TOKEN}", timeout=5) as response:
        data = json.loads(response.read().decode("utf-8"))
    print(f"   共 {data['total']} 台，画面在线 {data['camera_online']}，遥测在线 {data['mqtt_online']}")
    for item in data["printers"]:
        print(f"   · {item['name']:12} {item['state_text']:6} {item['progress']:>3}% "
              f"喷嘴 {item['nozzle']}/{item['nozzle_target']}℃ 热床 {item['bed']}/{item['bed_target']}℃ "
              f"跨度 {item['span']} 通道 {item['backend']}")
    if data["total"] != 8 or data["camera_online"] < 8:
        ok = False
        print("   ✗ 数量或在线数不符合预期（应为 8 台全部在线）")

    print("\n③ 首页 HTML")
    with urllib.request.urlopen(f"{base}/?token={TOKEN}", timeout=5) as response:
        html = response.read().decode("utf-8")
    print(f"   状态码 {response.status}，{len(html)} 字节，包含监控墙容器：{'id=\"wall\"' in html}")

    print("\n④ 单张快照 /api/frame/0")
    with urllib.request.urlopen(f"{base}/api/frame/0?token={TOKEN}", timeout=5) as response:
        frame = response.read()
    print(f"   {len(frame) // 1024} KB，JPEG 头 {frame[:2].hex()}，尾 {frame[-2:].hex()}")
    if not frame.startswith(b"\xff\xd8"):
        ok = False
        print("   ✗ 不是有效 JPEG")

    print("\n⑤ MJPEG 推流 /stream/0（读取 3 秒）")
    request = urllib.request.Request(f"{base}/stream/0?token={TOKEN}")
    with urllib.request.urlopen(request, timeout=8) as response:
        content_type = response.headers.get("Content-Type", "")
        print(f"   Content-Type: {content_type}")
        chunks = bytearray()
        started = time.time()
        while time.time() - started < 3:
            block = response.read(8192)
            if not block:
                break
            chunks += block
    boundaries = chunks.count(b"--bmframe")
    print(f"   收到 {len(chunks) // 1024} KB，分帧边界 {boundaries} 个")
    if "multipart/x-mixed-replace" not in content_type or boundaries < 2:
        ok = False
        print("   ✗ MJPEG 推流不符合预期")

    print("\n⑦ 单连接多路复用 /api/live（8 路画面，验证突破 6 连接限制）")
    request = urllib.request.Request(f"{base}/api/live?token={TOKEN}")
    frames: dict[int, int] = {}
    status_records = 0
    with urllib.request.urlopen(request, timeout=10) as response:
        content_type = response.headers.get("Content-Type", "")
        print(f"   Content-Type: {content_type}")
        buffer = bytearray()
        started = time.time()
        while time.time() - started < 4:
            block = response.read(16384)
            if not block:
                break
            buffer += block
            # 解析记录：BM(2) + 类型(1) + 序号(2 LE) + 长度(4 LE) + 负载
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
    print(f"   收到画面的路数：{len(frames)}（{sorted(frames.items())}）")
    print(f"   状态记录：{status_records} 条")
    if len(frames) < 8:
        ok = False
        print("   ✗ 并非所有画面都通过同一连接推流")
    else:
        print("   ✓ 8 路画面全部通过一条连接推送（浏览器 6 连接限制已绕过）")

    print("\n⑧ 新增状态字段与控制接口（仅对模拟器）")
    first = data["printers"][0]
    for key in ("ams", "external", "wifi", "wifi_level", "chamber", "finish_time", "light",
                "can_control", "hms", "active_tray"):
        present = key in first
        print(f"   字段 {key:12} 存在={present}", end="")
        if not present:
            ok = False
        if key == "ams":
            print(f"  槽位={len(first['ams'])} 例：{first['ams'][0] if first['ams'] else '无'}")
        elif key == "external":
            print(f"  外挂={first['external']}")
        elif key == "hms":
            print(f"  条数={len(first['hms'])}（最后一台应为 1 条）")
        else:
            print(f"  值={first.get(key)!r}")

    def post_command(index: int, action: str, value: str = ""):
        payload = json.dumps({"index": index, "action": action, "value": value}).encode()
        request = urllib.request.Request(
            f"{base}/api/command?token={TOKEN}", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    for action in ("light_off", "light_on", "pause", "resume", "stop"):
        try:
            status_code, result = post_command(0, action)
            print(f"   指令 {action:11} -> {status_code} {result.get('detail')}")
            if not result.get("ok"):
                ok = False
        except Exception as exc:  # noqa: BLE001
            print(f"   指令 {action:11} ✗ {exc}")
            ok = False

    time.sleep(2)
    with urllib.request.urlopen(f"{base}/api/printers?token={TOKEN}", timeout=8) as response:
        after = json.loads(response.read().decode("utf-8"))
    state_text = after["printers"][0]["state_text"]
    print(f"   指令后状态：{state_text}（期望「空闲」）")
    if "空闲" not in state_text:
        ok = False
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"{base}/api/command", data=b'{"index":0,"action":"pause"}',
                                   headers={"Content-Type": "application/json"}, method="POST"),
            timeout=5,
        )
        print("   无令牌下发指令：未被拒绝 ✗")
        ok = False
    except urllib.error.HTTPError as exc:
        print(f"   无令牌下发指令 -> {exc.code} ✓")

    print("\n⑥ 端口监听检查")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        listening = sock.connect_ex(("127.0.0.1", PORT)) == 0
    print(f"   {PORT} 端口可连接：{listening}")
    if not listening:
        ok = False
finally:
    server.stop()
    for session in sessions:
        session.stop()
    stop_simulator(printers, responder)

print("\n" + "=" * 60)
print("网页服务检查：" + ("全部通过 ✓" if ok else "存在失败项 ✗"))
print("=" * 60)
sys.exit(0 if ok else 1)
