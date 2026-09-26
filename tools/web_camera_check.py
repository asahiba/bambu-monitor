"""对着**真机**检查网页端的画面接口与详细读数（自己起一个临时服务，只读）。

用法::

    python tools/web_camera_check.py --printer 192.168.31.55 --port 7125
    python tools/web_camera_check.py --printer 192.168.31.110     # 拓竹（用访问代码）

与 `tools/web_live_check.py` 的区别：那个连的是**已经在跑的**服务（本机或 Docker），
这个自己起一个临时服务（绑 127.0.0.1 的随机端口、不写配置），专门验这几条：

1. ``GET /api/printers`` —— 状态 / 摄像头列表 / 详细读数 / 当前选了哪一路；
2. ``GET /api/frame/<i>`` 与 ``?cam=N`` —— 每路各取一帧，断言真的是 JPEG；
3. ``POST /api/camera {"action":"refresh"}`` —— 用户点「刷新视频流」走的就是这条；
4. ``POST /api/camera {"action":"select","camera":N}`` —— 切换看哪一路（多画面机器）。

**不下发任何会动机器的指令**（不暂停、不移动、不加热）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from app.bambu.models import PrinterInfo  # noqa: E402
from app.config import AppConfig  # noqa: E402
from app.core import create_session  # noqa: E402
from app.core.registry import FAMILY_BAMBU, MOONRAKER_FALLBACK_PORT  # noqa: E402
from tools._common import enable_utf8, lookup_code  # noqa: E402

TOKEN = "camera-check"


def _get(base: str, path: str) -> tuple[int, bytes]:
    request = urllib.request.Request(f"{base}{path}", headers={"User-Agent": "dsh-agent"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post(base: str, path: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except ValueError:
            return exc.code, {}


def main(argv: list[str] | None = None) -> int:
    enable_utf8()
    parser = argparse.ArgumentParser(description="对着真机检查网页端画面接口（只读）")
    parser.add_argument("--printer", required=True, help="设备 IP")
    parser.add_argument("--family", default="moonraker", help="设备族 id（默认 moonraker）")
    parser.add_argument("--port", type=int, default=0, help="服务端口（0 = 用该族默认端口）")
    parser.add_argument("--code", default="", help="拓竹的访问代码；不填则从配置里找")
    parser.add_argument("--wait", type=float, default=12.0, help="等首帧/首状态的秒数")
    args = parser.parse_args(argv)

    info = PrinterInfo(
        ip=args.printer,
        name=f"{args.family}@{args.printer}",
        family="" if args.family == FAMILY_BAMBU else args.family,
        port=args.port or (0 if args.family != FAMILY_BAMBU else 0),
        access_code=lookup_code(args.printer) if args.family == FAMILY_BAMBU else "",
    )
    if args.code:
        info.access_code = args.code
    config = AppConfig()
    config.persist = False  # 绝不写用户的真实配置
    config.printers = [info]

    session = create_session(info)
    session.set_max_fps(4.0)
    session.start()
    sessions = [session]

    from app.web.host import WebHost
    from app.web.server import WebServer

    host = WebHost(config, lambda: sessions)
    server = WebServer(
        get_sessions=lambda: sessions,
        port=0,
        token=TOKEN,
        fps=4.0,
        host="127.0.0.1",
        discover_fn=host.discover,
        add_printer_fn=host.add_printer,
        manage_printer_fn=host.manage_printer,
        export_config_fn=host.export_config_text,
        import_config_fn=host.import_config_text,
        diagnose_fn=host.diagnose,
        layout_fn=host.set_tile_span,
        camera_action_fn=host.camera_action,
        info_fn=host.info,
    )
    if not server.start():
        print("[失败] 服务没起来")
        return 1
    base = f"http://127.0.0.1:{server._httpd.server_address[1]}"  # noqa: SLF001
    print(f"临时网页服务：{base}（只读检查，不写配置）")
    failures: list[str] = []
    try:
        deadline = time.time() + max(2.0, args.wait)
        data: dict = {}
        while time.time() < deadline:
            status, body = _get(base, f"/api/printers?token={TOKEN}")
            if status == 200:
                data = json.loads(body)
                printers = data.get("printers") or []
                # 等**真的连上**（状态在线或至少拿到一帧）再下结论；
                # 以前只等状态文案变化，于是设备还没连上就往下走，
                # 结果第一次取帧必然 503（那是脚本等太早，不是功能坏了）
                if printers and printers[0].get("mqtt_online") and printers[0].get("camera_online"):
                    break
            time.sleep(0.2)
        # 状态与画面都稳定下来再取帧
        time.sleep(1.0)

        print("\n=== GET /api/printers ===")
        print(
            f"  共 {data.get('total')} 台 · 画面在线 {data.get('camera_online')} · "
            f"遥测在线 {data.get('mqtt_online')}"
        )
        item = (data.get("printers") or [{}])[0]
        print(f"  {item.get('name')}  {item.get('model')}")
        print(
            f"      状态={item.get('status_text')}  进度={item.get('progress')}%  "
            f"剩余={item.get('remaining_text')}  任务={item.get('task')!r}"
        )
        print(f"      画面通道={item.get('backend')}  当前第 {int(item.get('camera', 0)) + 1} 路")
        cameras = item.get("cameras") or []
        for cam in cameras:
            mark = "✓" if cam["available"] else "✗"
            print(f"      {mark} 摄像头[{cam['index']}] {cam['name']}  {cam['url']}")
        details = item.get("details") or []
        print(f"      详细读数 {len(details)} 条")
        for row in details:
            print(f"        {row['label']:18} {row['value']}")

        print("\n=== GET /api/frame/<i> ===")
        for cam in cameras or [{"index": 0, "name": "默认", "available": True}]:
            query = f"?cam={cam['index']}&token={TOKEN}" if cam["index"] else f"?token={TOKEN}"
            status, body = _get(base, f"/api/frame/0{query}")
            ok = status == 200 and body.startswith(b"\xff\xd8")
            print(f"  {'✓' if ok else '✗'} cam={cam['index']} HTTP {status} {len(body) / 1024:.1f} KB")
            if not ok:
                failures.append(f"取 cam={cam['index']} 失败：HTTP {status}")

        print("\n=== POST /api/camera（刷新视频流） ===")
        status, result = _post(base, f"/api/camera?token={TOKEN}", {"index": 0, "action": "refresh"})
        print(f"  HTTP {status} {result.get('detail')}（{len(result.get('cameras') or [])} 路）")
        if not result.get("ok"):
            failures.append(f"刷新失败：{result}")
        time.sleep(1.0)
        status, body = _get(base, f"/api/frame/0?token={TOKEN}")
        print(f"  刷新后取帧：HTTP {status} {len(body) / 1024:.1f} KB")
        if not (status == 200 and body.startswith(b"\xff\xd8")):
            failures.append("刷新后取不到画面")

        if len(cameras) > 1:
            print("\n=== POST /api/camera（切换看哪一路） ===")
            for target in (1, 0):
                status, result = _post(
                    base,
                    f"/api/camera?token={TOKEN}",
                    {"index": 0, "action": "select", "camera": target},
                )
                print(f"  切到第 {target + 1} 路：HTTP {status} {result.get('detail')}")
                time.sleep(0.8)
                status, body = _get(base, f"/api/frame/0?token={TOKEN}")
                print(f"      取帧 HTTP {status} {len(body) / 1024:.1f} KB")
    finally:
        server.stop()
        for session in sessions:
            session.stop()

    print()
    if failures:
        print("[失败] " + "；".join(failures))
        return 1
    print("✓ 网页端画面接口检查通过（只用了只读请求与画面刷新/切换）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
