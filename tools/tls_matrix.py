"""开发用：对真实打印机的某个端口尝试多种 TLS 配置，找出可行的握手参数。

用法：``python tools/tls_matrix.py 192.168.31.27 6000 322``
"""

from __future__ import annotations

import os
import socket
import ssl
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools._common import enable_utf8  # noqa: E402

#: 下面会打印 ✓；Windows 默认 GBK 控制台或输出被重定向时会 UnicodeEncodeError
enable_utf8()

from app.bambu.tlsutil import BBL_CA_FILE  # noqa: E402


def insecure(**kwargs) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    for key, value in kwargs.items():
        setattr(context, key, value)
    return context


def verifying(max_version=None) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=BBL_CA_FILE)
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    context.check_hostname = False
    if max_version is not None:
        context.maximum_version = max_version
    return context


def build_variants() -> list[tuple[str, ssl.SSLContext]]:
    variants: list[tuple[str, ssl.SSLContext]] = []
    variants.append(("默认(不校验, 不限版本)", insecure()))
    variants.append(("TLS1.2 上限(不校验)", insecure(maximum_version=ssl.TLSVersion.TLSv1_2)))
    variants.append(
        (
            "TLS1.2 上限 + SECLEVEL=0(不校验)",
            insecure(maximum_version=ssl.TLSVersion.TLSv1_2),
        )
    )
    variants.append(("仅 TLS1.3(不校验)", insecure(minimum_version=ssl.TLSVersion.TLSv1_3)))
    variants.append(("校验 BBL CA + TLS1.2 上限", verifying(ssl.TLSVersion.TLSv1_2)))
    variants.append(("校验 BBL CA + 不限版本", verifying()))
    return variants


def main(host: str, ports: list[int]) -> int:
    for port in ports:
        print(f"\n=== {host}:{port} ===")
        for label, context in build_variants():
            if "SECLEVEL" in label:
                try:
                    context.set_ciphers("DEFAULT:@SECLEVEL=0")
                except ssl.SSLError as exc:
                    print(f"  {label:<28} 跳过（{exc}）")
                    continue
            try:
                sock = socket.create_connection((host, port), timeout=4)
            except OSError as exc:
                print(f"  {label:<28} TCP 不可达（{exc}）")
                continue
            try:
                tls = context.wrap_socket(sock, server_hostname=host)
                version = tls.version()
                cipher = tls.cipher()[0] if tls.cipher() else "?"
                print(f"  {label:<28} 成功 ✓  {version} / {cipher}")
                tls.close()
            except Exception as exc:  # noqa: BLE001
                print(f"  {label:<28} 失败 ✗  {exc.__class__.__name__}: {exc}")
                try:
                    sock.close()
                except OSError:
                    pass
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], [int(p) for p in sys.argv[2:]]))
