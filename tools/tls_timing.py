"""开发用：测量打印机 TLS 各候选参数的握手耗时（验证上下文缓存效果）。

用法：``python tools/tls_timing.py <IP> [端口]``
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu import tlsutil  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

enable_utf8()

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.31.110"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 6000

print(f"=== {host}:{port} ===")
print("逐个候选参数单独计时：")
for index, (context, verified) in enumerate(tlsutil.context_candidates(True)):
    started = time.time()
    try:
        import socket as _socket

        raw = _socket.create_connection((host, port), timeout=6.0)
        raw.settimeout(tlsutil.HANDSHAKE_TIMEOUT)
        tls = context.wrap_socket(raw, server_hostname=host)
        tls.close()
        print(f"  候选{index}（{'校验' if verified else '不校验'}）: 成功，用时 {time.time() - started:.2f}s")
    except Exception as exc:  # noqa: BLE001
        print(
            f"  候选{index}（{'校验' if verified else '不校验'}）: 失败，用时 {time.time() - started:.2f}s -> "
            f"{exc.__class__.__name__}: {str(exc)[:60]}"
        )

print("\n使用带缓存的 connect_tls（第一次会自动摸索、之后走缓存）：")
for round_index in range(3):
    started = time.time()
    try:
        tls, verified = tlsutil.connect_tls(host, port, timeout=6.0, server_hostname=host)
        tls.close()
        print(f"  第 {round_index + 1} 次: 成功，用时 {time.time() - started:.2f}s，证书链{'已校验' if verified else '未校验'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  第 {round_index + 1} 次: 失败，用时 {time.time() - started:.2f}s -> {exc}")
