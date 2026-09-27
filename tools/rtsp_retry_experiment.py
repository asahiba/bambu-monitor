"""实验：对同一台打印机做「慢节奏」与「快节奏」TLS 探测，比较成功率。

背景：三台 X2D 的 322 端口 TCP 可连但握手经常没响应（我们自己的日志里能看到
"RTSPS 预探第 19 次才恢复响应"）。要判断的是：**我们快速重试这件事本身，
是不是把打印机的实时画面服务压得更难应答**。

做法（全程只做 TLS 握手 + 一条只读的 DESCRIBE）：

1. 慢节奏：每 10 秒探一次，共 6 次；
2. 快节奏：每 1 秒探一次，共 30 次；
3. 各自统计成功次数与耗时。

如果快节奏的成功率明显更低，说明重试太快会互相抢（同一台打印机的 TLS 会话
数量很有限），那就该改成"先快后慢"的退避，而不是一直猛冲。
"""

from __future__ import annotations

import argparse
import os
import socket
import ssl
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu.ports import RTSP_PORT  # noqa: E402
from tools._common import enable_utf8, lookup_code  # noqa: E402


def probe(host: str, timeout: float = 3.0) -> tuple[bool, float, str]:
    """一次 TLS 握手 + DESCRIBE（只读）。返回 (成功, 耗时, 说明)。"""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    url = f"rtsps://{host}:{RTSP_PORT}/streaming/live/1"
    request = (
        f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n"
    ).encode()
    started = time.time()
    tls = None
    try:
        raw = socket.create_connection((host, RTSP_PORT), timeout=timeout)
        raw.settimeout(timeout)
        tls = context.wrap_socket(raw, server_hostname=host)
        tls.settimeout(timeout)
        tls.sendall(request)
        data = tls.recv(1024).decode("utf-8", "replace")
        status = data.splitlines()[0].strip() if data else "（空）"
        return True, time.time() - started, status
    except Exception as exc:  # noqa: BLE001
        return False, time.time() - started, f"{type(exc).__name__}"
    finally:
        if tls is not None:
            try:
                tls.close()
            except OSError:
                pass


def run(host: str, label: str, interval: float, count: int) -> tuple[int, float]:
    print(f"\n{label}（间隔 {interval:g} 秒 × {count} 次）")
    ok = 0
    total = 0.0
    for index in range(count):
        success, elapsed, note = probe(host)
        total += elapsed
        ok += 1 if success else 0
        print(f"  {index + 1:3}. {'✓' if success else '✗'} {elapsed:5.1f}s  {note}")
        if index + 1 < count:
            time.sleep(max(0.0, interval - elapsed))
    print(f"  → 成功 {ok}/{count}，平均耗时 {total / count:.1f}s")
    return ok, total / count


def main(argv: list[str] | None = None) -> int:
    enable_utf8()
    parser = argparse.ArgumentParser(description="比较慢/快两种探测节奏的成功率（只读）")
    parser.add_argument("host")
    parser.add_argument("--code", default="--auto")
    args = parser.parse_args(argv)
    if not lookup_code(args.host) and args.code == "--auto":
        print("提示：配置里没有这台设备的凭据（本实验不需要凭据，继续）")

    slow_ok, slow_avg = run(args.host, "① 慢节奏", interval=10.0, count=6)
    fast_ok, fast_avg = run(args.host, "② 快节奏", interval=1.0, count=30)

    print("\n=== 结论 ===")
    print(f"  慢节奏成功率 {slow_ok}/6（平均 {slow_avg:.1f}s）")
    print(f"  快节奏成功率 {fast_ok}/30（平均 {fast_avg:.1f}s）")
    if fast_ok / 30 + 0.15 < slow_ok / 6:
        print("  ⚠️ 快节奏明显更差 → 重试太快会互相抢，应当改成'先快后慢'的退避")
    elif slow_ok == 0 and fast_ok == 0:
        print("  两种都探不通：这台打印机的实时画面服务当前不工作（需要看打印机本身）")
    else:
        print("  快节奏没有更差：重试频率不是主因，可以保持较快的重试")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
