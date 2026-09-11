"""开发用：对真实打印机做只读连通性检查（不发送任何鉴权/控制指令）。

用法：``python tools/real_printer_check.py 192.168.31.27 01P09A470310013``
"""

from __future__ import annotations

import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from app.bambu import tlsutil  # noqa: E402


def check_port(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError as exc:
        print(f"  {port:>5} 端口: 不可达（{exc.__class__.__name__}: {exc}）")
        return False


def main(hosts: list[str]) -> int:
    for host in hosts:
        print(f"\n=== {host} ===")
        for port in (8883, 6000, 322, 80):
            if check_port(host, port):
                print(f"  {port:>5} 端口: 可连接 ✓")
        verified = tlsutil.can_verify(host, 8883, timeout=4.0)
        print(f"  证书链校验（Bambu Lab CA）: {'通过 ✓' if verified else '未通过（将使用不校验证书模式）'}")
        frame = None
        try:
            sock, ok = tlsutil.connect_tls(host, 6000, timeout=4.0)
            frame = ok
            sock.close()
        except Exception as exc:  # noqa: BLE001
            print(f"  6000 端口 TLS 握手: 失败（{exc}）")
        else:
            print(f"  6000 端口 TLS 握手: 成功（证书链{'已校验' if frame else '未校验'}）")
    return 0


if __name__ == "__main__":
    targets = sys.argv[1:]
    if not targets:
        print("请给出至少一个打印机 IP")
        sys.exit(2)
    sys.exit(main(targets))
