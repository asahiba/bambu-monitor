"""只读摸清一台 Moonraker 设备：能拿到什么、缺什么。

用法::

    python tools/moonraker_probe.py 192.168.31.55 [--port 7125] [--api-key KEY]
    python tools/moonraker_probe.py 192.168.31.55 --camera-check

它只发 **GET** 和 Moonraker 官方规定的只读查询（``POST /printer/objects/query``），
**不下发任何会动机器的指令**。

打印的内容：

1. ``/server/info``、``/printer/info``：版本、klippy 状态；
2. ``/server/webcams/list``：摄像头原始配置（名字、URL、帧率、服务）；
3. **每个摄像头 URL 两种解析方式各试一次**（相对 Moonraker 端口 / 相对主机 80 端口），
   看哪种真的能拿到 JPEG —— 这决定了程序里该怎么拼 URL；
4. ``/printer/objects/list``：设备全部对象（据此判断还能显示什么）；
5. 用程序当前那份对象清单查一次状态，并打印程序解析出来的结果，
   好看出**哪些信息没被取到**；
6. ``/server/files/list``：可打印的文件列表（做「开始打印」功能要用）。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.adapters.moonraker.adapter import QUERY_OBJECTS, parse_status  # noqa: E402
from app.core.registry import MOONRAKER_FALLBACK_PORT  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

TIMEOUT = 8.0


def _request(
    url: str, *, payload: dict | None = None, api_key: str = "", limit: int = 0
) -> tuple[int, str, bytes]:
    """发一次请求。``limit > 0`` 时只读这么多字节就断开。

    ⚠️ MJPEG 的 ``?action=stream`` 是**永不结束**的响应：不留 ``limit``
    就会一直卡在 ``read()`` 上（本工具第一版就这么挂住的）。
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if api_key:
        request.add_header("X-Api-Key", api_key)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read(limit) if limit else response.read()
            return response.status, response.headers.get("Content-Type", ""), body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", "") if exc.headers else "", exc.read(4096)
    except (urllib.error.URLError, OSError) as exc:
        return 0, f"连接失败：{exc}", b""


def _get_json(base: str, path: str, api_key: str) -> dict:
    status, _ctype, body = _request(f"{base}{path}", api_key=api_key)
    if status != 200:
        print(f"  {path} -> HTTP {status} {body[:120]!r}")
        return {}
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        print(f"  {path} -> 不是 JSON：{body[:120]!r}")
        return {}


def _describe_image(body: bytes, ctype: str) -> str:
    kind = "JPEG" if body.startswith(b"\xff\xd8") else ("PNG" if body.startswith(b"\x89PNG") else "非图片")
    return f"{ctype.split(';')[0]:24} {len(body):>8} 字节  {kind}"


def _camera_url_variants(host: str, port: int, url: str) -> list[tuple[str, str]]:
    """把摄像头 URL 的两种可能来源都列出来。"""
    if url.startswith("http://") or url.startswith("https://"):
        return [("绝对地址（配置里就是完整的）", url)]
    path = url if url.startswith("/") else "/" + url
    return [
        ("主机 80 端口（crowsnest / 前端静态目录）", f"http://{host}{path}"),
        (f"Moonraker 端口 {port}", f"http://{host}:{port}{path}"),
    ]


def _camera_scan(host: str, port: int, api_key: str) -> None:
    """扫一遍常见的摄像头端点：Moonraker 里只登记了一个，机器上未必只有一个。

    crowsnest 会按 ``[cam X]`` 段生成 ``/webcam/`` ``/webcam2/`` …；
    ustreamer 自己也可能直接监听 8080。全部只用 GET 探一帧，不动设备。
    """
    candidates: list[tuple[str, str]] = []
    for index in range(1, 6):
        name = "webcam" if index == 1 else f"webcam{index}"
        candidates.append((f"/{name}/（nginx 反代，端口 80）", f"http://{host}/{name}/?action=snapshot"))
        for extra in (8080 + index - 1, 8081, 8090):
            candidates.append(
                (f"ustreamer 直连 端口 {extra}", f"http://{host}:{extra}/?action=snapshot")
            )
    candidates.append(("Moonraker 端口上的 /webcam/", f"http://{host}:{port}/webcam/?action=snapshot"))

    print("\n=== 摄像头端点扫描（只发 GET，各取一帧） ===")
    seen: set[str] = set()
    found = 0
    for label, url in candidates:
        if url in seen:
            continue
        seen.add(url)
        status, ctype, body = _request(url, api_key=api_key, limit=200000)
        if status == 200 and body.startswith(b"\xff\xd8"):
            found += 1
            print(f"  ✓ {label:28} {url}")
            print(f"      {_describe_image(body, ctype)}")
        elif status not in (0, 404):
            print(f"  ? {label:28} HTTP {status}  {_describe_image(body, ctype)}")
    if not found:
        print("  一个都没探到（摄像头可能没在跑，或者用的是别的路径）")


def main(argv: list[str] | None = None) -> int:
    enable_utf8()
    parser = argparse.ArgumentParser(description="只读摸清一台 Moonraker 设备")
    parser.add_argument("host")
    parser.add_argument("--port", type=int, default=MOONRAKER_FALLBACK_PORT)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--camera-check", action="store_true", help="下载一帧并报告大小/类型")
    parser.add_argument("--camera-scan", action="store_true", help="扫常见的多摄像头端点")
    parser.add_argument("--objects", default="", help="额外查询的对象，逗号分隔")
    args = parser.parse_args(argv)

    base = f"http://{args.host}:{args.port}"
    print(f"=== {base} ===")

    info = _get_json(base, "/server/info", args.api_key).get("result", {})
    print(f"Moonraker {info.get('moonraker_version', '?')} / "
          f"Klipper {info.get('klipper_version') or '（未上报）'} / "
          f"klippy={info.get('klippy_state', '?')}")
    for key in ("warnings", "failed_components", "websocket_count", "api_version"):
        if info.get(key):
            print(f"  {key}: {info[key]}")

    printer_info = _get_json(base, "/printer/info", args.api_key).get("result", {})
    if printer_info:
        print("printer/info:")
        for key, value in printer_info.items():
            text = str(value)
            print(f"  {key:18} {text[:110]}{'…' if len(text) > 110 else ''}")

    print("\n=== 摄像头（/server/webcams/list 原样） ===")
    webcams = _get_json(base, "/server/webcams/list", args.api_key).get("result", {}).get("webcams", [])
    if not webcams:
        print("  空 —— Moonraker 里没有配置可读的摄像头")
    for cam in webcams:
        print(json.dumps(cam, ensure_ascii=False, indent=2))

    if args.camera_check and webcams:
        print("\n=== 摄像头取帧实测（只发 GET） ===")
        for cam in webcams:
            name = cam.get("name", "?")
            print(f"\n[{name}] service={cam.get('service')} enabled={cam.get('enabled')}")
            for field in ("snapshot_url", "stream_url"):
                url = str(cam.get(field, "") or "")
                if not url:
                    continue
                print(f"  {field} = {url!r}")
                # 流地址是持续响应：只读开头一段就断开
                limit = 8192 if field == "stream_url" else 0
                for label, candidate in _camera_url_variants(args.host, args.port, url):
                    status, ctype, body = _request(candidate, api_key=args.api_key, limit=limit)
                    ok = status == 200 and (body.startswith(b"\xff\xd8") or "image" in ctype)
                    mark = "✓" if ok else " "
                    print(f"    {mark} {label:38} HTTP {status}  {_describe_image(body, ctype)}")
                    if ok:
                        print(f"        {candidate}")

    if args.camera_scan:
        _camera_scan(args.host, args.port, args.api_key)

    print("\n=== 设备对象（/printer/objects/list） ===")
    objects = _get_json(base, "/printer/objects/list", args.api_key).get("result", {}).get("objects", [])
    print(f"  共 {len(objects)} 个：{'、'.join(objects)}")

    print("\n=== 程序当前查询的对象 ===")
    print(f"  {list(QUERY_OBJECTS)}")
    wanted = list(QUERY_OBJECTS)
    if args.objects:
        wanted.extend(item.strip() for item in args.objects.split(",") if item.strip())
    status, _ctype, body = _request(
        f"{base}/printer/objects/query",
        payload={"objects": {name: None for name in wanted}},
        api_key=args.api_key,
    )
    if status == 200:
        raw = json.loads(body.decode("utf-8", errors="replace"))
        parsed = parse_status(raw)
        print("\n  程序解析出来的字段：")
        for key, value in sorted(parsed.items()):
            print(f"    {key:24} {value}")
        missing = [name for name in wanted if name not in (raw.get("result", {}).get("status") or {})]
        if missing:
            print(f"  ⚠️ 设备没有回应这些对象：{missing}")
        status_objects = raw.get("result", {}).get("status") or {}
        print("\n  设备实际返回的对象：")
        for name, value in status_objects.items():
            text = json.dumps(value, ensure_ascii=False)
            print(f"    {name:20} {text[:150]}{'…' if len(text) > 150 else ''}")
    else:
        print(f"  查询失败：HTTP {status}")

    print("\n=== 可打印文件（/server/files/list?root=gcodes） ===")
    files = _get_json(base, "/server/files/list?root=gcodes", args.api_key).get("result", [])
    print(f"  共 {len(files)} 个")
    for item in files[:10]:
        print(f"    {item.get('path')}  {item.get('size', 0) / 1024:.0f} KB  "
              f"{item.get('modified', '')}  {item.get('print_start_time') or ''}")
    if len(files) > 10:
        print(f"    …还有 {len(files) - 10} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
