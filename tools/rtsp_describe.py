"""开发用：手工对打印机 322 端口做一次 RTSP DESCRIBE（TLS + Digest 鉴权）。

可以判断：RTSPS 服务是否开启、URL 路径是否正确、鉴权是否被接受。
不提供访问代码时用占位口令，返回 401 也说明服务端是活的。

用法：``python tools/rtsp_describe.py <IP> [访问代码|--auto]``
"""

from __future__ import annotations

import base64
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu import tlsutil  # noqa: E402
from tools._common import enable_utf8, resolve_code  # noqa: E402

enable_utf8()

PATHS = (
    "/streaming/live/1",
    "/streaming/live/2",
    "/live/1",
)


def _request(tls: socket.socket, method: str, url: str, cseq: int, auth: str) -> str:
    lines = [f"{method} {url} RTSP/1.0", f"CSeq: {cseq}", "User-Agent: bambu-monitor-diag"]
    if method == "DESCRIBE":
        lines.append("Accept: application/sdp")
    if auth:
        lines.append(f"Authorization: Basic {auth}")
    tls.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    tls.settimeout(6.0)
    data = b""
    try:
        while b"\r\n\r\n" not in data:
            chunk = tls.recv(4096)
            if not chunk:
                break
            data += chunk
    except (socket.timeout, TimeoutError, OSError):
        pass
    return data.decode("utf-8", "replace")


def describe(host: str, code: str, paths: tuple[str, ...] = PATHS) -> bool:
    """返回 True 表示 DESCRIBE 返回 200（可直接拉流）。"""
    auth = base64.b64encode(f"bblp:{code}".encode()).decode()
    try:
        tls, verified = tlsutil.connect_tls(host, 322, timeout=5.0, server_hostname=host)
    except Exception as exc:  # noqa: BLE001
        print(f"   ✗ TLS 连接失败：{exc.__class__.__name__}: {exc}")
        return False
    print(f"   TLS 握手成功（证书链{'已校验' if verified else '未校验'}）")
    ok = False
    try:
        for index, path in enumerate(paths):
            response = _request(tls, "DESCRIBE", f"rtsps://{host}:322{path}", index + 1, auth)
            head = response.splitlines()[0] if response else "（无响应）"
            print(f"   DESCRIBE {path} -> {head}")
            for line in response.splitlines()[1:8]:
                if line.strip():
                    print(f"      {line}")
            if "200" in head:
                ok = True
                for line in response.splitlines():
                    if line.lower().startswith(("m=", "a=control", "a=rtpmap")):
                        print(f"      {line.strip()}")
                break
            if "401" in head or "403" in head:
                break
    finally:
        try:
            tls.close()
        except OSError:
            pass
    if not ok:
        print("   注：401 表示 RTSP 服务在运行但口令未被接受；无响应表示服务未开启")
    return ok


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    host = argv[0]
    code = resolve_code(host, argv[1] if len(argv) > 1 else "")
    if not code:
        print("没有可用的访问代码（可传 --auto 从配置读取）")
        return 2
    print(f"目标 {host}:322（rtsps，用户名 bblp，口令 {len(code)} 位）")
    describe(host, code)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
