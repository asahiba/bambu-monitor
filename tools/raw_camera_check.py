"""开发用：直接连接 6000 端口，验证 TLS + 鉴权包 + 帧格式。

用法：``python tools/raw_camera_check.py <IP> [访问代码|--auto]``
"""

from __future__ import annotations

import os
import socket
import ssl
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools._common import enable_utf8, resolve_code  # noqa: E402

enable_utf8()

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.2"
CODE = resolve_code(HOST, sys.argv[2] if len(sys.argv) > 2 else "")
if not CODE:
    print("没有可用的访问代码（可传 --auto 从配置读取，或直接给出）")
    sys.exit(2)

print(f"connecting {HOST}:6000 ...", flush=True)
sock = socket.create_connection((HOST, 6000), timeout=6)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
context.maximum_version = ssl.TLSVersion.TLSv1_2
tls = context.wrap_socket(sock, server_hostname="test")
print("tls ok", flush=True)

packet = bytearray()
packet += struct.pack("<IIII", 0x40, 0x3000, 0, 0)
packet += b"bblp".ljust(32, b"\x00")
packet += CODE.encode().ljust(32, b"\x00")
tls.sendall(bytes(packet))
print(f"auth packet sent ({len(packet)} bytes)", flush=True)

header = b""
while len(header) < 16:
    chunk = tls.recv(16 - len(header))
    if not chunk:
        print("connection closed by printer (wrong access code?)", flush=True)
        sys.exit(2)
    header += chunk
size = struct.unpack_from("<I", header, 0)[0]
print(f"frame header: size={size}", flush=True)

payload = b""
while len(payload) < size:
    chunk = tls.recv(min(65536, size - len(payload)))
    if not chunk:
        print("closed mid-frame", flush=True)
        sys.exit(3)
    payload += chunk
print(f"jpeg ok: {len(payload)} bytes, soi={payload[:2].hex()} eoi={payload[-2:].hex()}", flush=True)
tls.close()
