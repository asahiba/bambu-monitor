"""开发用：用错误口令观察 6000 端口对鉴权包的原始反应。

可以区分「不支持该协议」与「只是口令不对」：
* 立刻关闭连接（recv 返回空）→ 协议存在，只是口令被拒
* 超时无任何数据 → 该端口不是这套 JPEG 流协议，或服务未就绪
* 收到数据 → 值得进一步分析

用法：``python tools/probe6000.py <IP> [IP...]``
"""

from __future__ import annotations

import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu import tlsutil  # noqa: E402
from app.bambu.camera import build_auth_packet  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

enable_utf8()


def probe(host: str, code: str = "00000000") -> None:
    print(f"\n=== {host}:6000（口令 {code}）===")
    started = time.time()
    try:
        tls, verified = tlsutil.connect_tls(host, 6000, timeout=5.0, server_hostname=host)
    except Exception as exc:  # noqa: BLE001
        print(f"  TLS 连接失败：{exc.__class__.__name__}: {exc}")
        return
    print(f"  TLS 握手成功（{time.time() - started:.2f}s，证书链{'已校验' if verified else '未校验'}）")
    try:
        tls.sendall(build_auth_packet(code))
        tls.settimeout(6.0)
        try:
            data = tls.recv(4096)
        except (socket.timeout, TimeoutError):
            print("  发送鉴权包后 6 秒内没有任何数据 → 该端口不提供这套 JPEG 流（或服务未就绪）")
            return
        except OSError as exc:
            print(f"  读取异常：{exc}")
            return
        if not data:
            print("  连接被服务端立即关闭 → 协议存在，口令被拒绝 ✓（口令正确时应开始推帧）")
            return
        print(f"  收到 {len(data)} 字节：{data[:32].hex(' ')}")
        if len(data) >= 16:
            import struct

            size = struct.unpack_from("<I", data, 0)[0]
            print(f"  首 4 字节作为帧长解析 = {size}（合理范围应为 512 ~ 8MB）")
    finally:
        try:
            tls.close()
        except OSError:
            pass


def main(argv: list[str]) -> int:
    hosts = argv or ["192.168.31.110"]
    for host in hosts:
        probe(host)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
