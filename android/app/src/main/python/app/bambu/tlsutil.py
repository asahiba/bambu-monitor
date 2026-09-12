"""打印机 TLS 连接工具。

拓竹打印机的自签证书由 Bambu Lab CA 签发，证书 CN 为打印机序列号，因此需要：

* 关闭主机名校验（连接用的是 IP，CN 是序列号）
* 关闭 Python 3.13+ 的 X509 严格校验（拓竹 CA 缺少 key usage 扩展）
* 把 TLS 上限压到 1.2（部分固件不回应 TLS 1.3 ClientHello，会一直挂住）

另外实测发现：**X1 系列 6000 端口的 TLS 服务使用的密钥/证书低于 OpenSSL 3
默认安全级别（SECLEVEL=1）**，直接握手会收到 `sslv3 alert handshake failure`，
必须把安全级别降到 0 才能连上。因此这里按「先安全、后兼容」的顺序逐个尝试：

1. 校验证书链 + 默认安全级别（P1/A1、H2 等较新固件）
2. 校验证书链 + SECLEVEL=0
3. 不校验证书 + 默认安全级别
4. 不校验证书 + SECLEVEL=0（X1 系列 6000 端口）
"""

from __future__ import annotations

import os
import socket
import ssl
from typing import Optional

CERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
BBL_CA_FILE = os.path.join(CERT_DIR, "ca_cert.pem")


def _apply_common(context: ssl.SSLContext) -> ssl.SSLContext:
    try:
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    except (ValueError, AttributeError):  # pragma: no cover
        pass
    return context


def _lower_security_level(context: ssl.SSLContext) -> ssl.SSLContext:
    try:
        context.set_ciphers("DEFAULT:@SECLEVEL=0")
    except ssl.SSLError:  # pragma: no cover
        pass
    return context


def make_context(verify: bool = True, legacy: bool = False) -> ssl.SSLContext:
    """创建用于打印机连接的 TLS 上下文。

    :param verify: 是否用内置的 Bambu Lab CA 校验证书链
    :param legacy: 是否把 OpenSSL 安全级别降到 0（兼容 X1 系列的摄像头服务）
    """
    if verify and os.path.exists(BBL_CA_FILE):
        context = ssl.create_default_context(cafile=BBL_CA_FILE)
        # Python 3.13 起默认开启严格校验，拓竹 CA 缺少 key usage 扩展会报错
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        context.check_hostname = False
    else:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    _apply_common(context)
    if legacy:
        _lower_security_level(context)
    return context


def context_candidates(verify_chain: bool = True) -> list[tuple[ssl.SSLContext, bool]]:
    """返回 (上下文, 是否已校验证书链) 的候选列表，按安全性从高到低。"""
    candidates: list[tuple[ssl.SSLContext, bool]] = []
    if verify_chain and os.path.exists(BBL_CA_FILE):
        candidates.append((make_context(True, legacy=False), True))
        candidates.append((make_context(True, legacy=True), True))
    candidates.append((make_context(False, legacy=False), False))
    candidates.append((make_context(False, legacy=True), False))
    return candidates


#: 记住每台打印机上次成功的 TLS 参数，避免每次都把不可用的组合重试一遍。
#: 实测 X2D 的 6000 端口握手要 5 秒以上，重复试探会直接拖垮连接建立。
_CONTEXT_CACHE: dict[tuple[str, int], tuple[ssl.SSLContext, bool]] = {}

#: TLS 握手可能比 TCP 连接慢得多（X2D 实测约 5.7 秒），因此单独给足时间
HANDSHAKE_TIMEOUT = 15.0


def _ordered_candidates(
    host: str, port: int, verify_chain: bool
) -> list[tuple[ssl.SSLContext, bool]]:
    cached = _CONTEXT_CACHE.get((host, port))
    candidates = context_candidates(verify_chain)
    if cached is None:
        return candidates
    ordered = [cached]
    ordered.extend(item for item in candidates if item[0] is not cached[0])
    return ordered


def connect_tls(
    host: str,
    port: int,
    timeout: float = 6.0,
    server_hostname: Optional[str] = None,
    verify_chain: bool = True,
    source_address=None,
) -> tuple[ssl.SSLSocket, bool]:
    """建立到打印机的 TLS 连接，自动挑选可用的安全参数。

    返回 (socket, verified)；verified=False 表示用的是不校验证书的模式。
    """
    sni = server_hostname or host
    last_error: Optional[BaseException] = None
    for context, verified in _ordered_candidates(host, port, verify_chain):
        raw = None
        try:
            raw = socket.create_connection(
                (host, port), timeout=timeout, source_address=source_address
            )
            raw.settimeout(HANDSHAKE_TIMEOUT)
            tls = context.wrap_socket(raw, server_hostname=sni)
            tls.settimeout(None)
            _CONTEXT_CACHE[(host, port)] = (context, verified)
            return tls, verified
        except (ssl.SSLError, ssl.CertificateError, OSError) as exc:
            last_error = exc
            if raw is not None:
                try:
                    raw.close()
                except OSError:
                    pass
            continue
    raise last_error if last_error is not None else OSError("TLS 连接失败")


def select_context(
    host: str, port: int, server_hostname: Optional[str] = None, timeout: float = 4.0
) -> tuple[ssl.SSLContext, bool]:
    """为 MQTT 这类「先给上下文、后建连」的库预选一个可用的 TLS 上下文。

    返回 (上下文, 是否已校验证书链)。
    """
    sni = server_hostname or host
    fallback = make_context(False, legacy=True)
    for context, verified in _ordered_candidates(host, port, True):
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.settimeout(HANDSHAKE_TIMEOUT)
            tls = context.wrap_socket(sock, server_hostname=sni)
            try:
                tls.close()
            except OSError:
                pass
            _CONTEXT_CACHE[(host, port)] = (context, verified)
            return context, verified
        except (ssl.SSLError, ssl.CertificateError, OSError):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            continue
    return fallback, False


def can_verify(host: str, port: int, server_hostname: Optional[str] = None, timeout: float = 3.0) -> bool:
    """该端口的证书链能否通过 Bambu Lab CA 校验。"""
    _, verified = select_context(host, port, server_hostname, timeout)
    return verified
