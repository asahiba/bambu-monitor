"""极简 WebSocket 客户端（JSON-RPC 2.0），只用标准库。

## 为什么需要它

Moonraker 有三类接口**只能走 WebSocket**（官方源码里带
``transports = all & ~HTTP``）：

1. ``/printer/emergency_stop`` —— 急停
2. 整个 ``/printer/control/*`` 系列 —— 灯光、风扇、温度、速度倍率
3. **Snapmaker U1 的摄像头保活** ``camera.start_monitor`` ——
   不周期性发它，``monitor.jpg`` 就一直是陈旧的旧帧（真机实测确认）

而遥测与控制的主体（``print/pause|resume|cancel``）走 HTTP 就够了。

## 为什么不用第三方库

项目对第三方依赖一直很克制（运行依赖只有 PySide6 / paho-mqtt / opencv / cryptography），
且这个客户端的用途非常窄：连一条 WS、发 JSON-RPC、收结果。用标准库实现
（socket + 手工握手 + 帧编解码）比引一个库更好维护，也避免打包体积增加。

## 实现范围

只实现 Moonraker 需要的部分：**客户端掩码、文本帧、分片聚合**。
不做 permessage-deflate、不做服务端角色、不发大帧（请求都很小）。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import ssl
import struct
import threading
import time
from typing import Any, Optional
from urllib.parse import urlparse

LOGGER = logging.getLogger("bambu-monitor.ws")

#: WebSocket 帧的操作码
OP_CONT = 0x0
OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: 单帧上限（防畸形数据把内存吃满）
MAX_FRAME = 4 * 1024 * 1024


class WebSocketError(RuntimeError):
    """握手或帧解析失败。"""


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    """读满 count 字节；对端关闭时抛 WebSocketError。"""
    buffer = bytearray()
    while len(buffer) < count:
        chunk = sock.recv(count - len(buffer))
        if not chunk:
            raise WebSocketError("连接已被对端关闭")
        buffer += chunk
    return bytes(buffer)


def encode_frame(opcode: int, payload: bytes, *, mask: bool = True) -> bytes:
    """编码一帧。

    ``mask=True``（默认）用于**客户端**——RFC 6455 规定客户端必须给负载加掩码；
    测试用的假服务端要传 ``mask=False``。
    """
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append((0x80 if mask else 0) | length)
    elif length < 65536:
        header.append((0x80 if mask else 0) | 126)
        header += struct.pack(">H", length)
    else:
        header.append((0x80 if mask else 0) | 127)
        header += struct.pack(">Q", length)
    if not mask:
        return bytes(header) + payload
    key = os.urandom(4)
    masked = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + key + masked


def read_frame(sock: socket.socket, *, expect_masked: bool = False) -> tuple[int, bytes]:
    """读一帧并聚合分片，返回 ``(opcode, payload)``。

    ``expect_masked=True`` 用于服务端角色（读客户端发来的帧，它们必然带掩码）。
    ping 会被自动回 pong 后跳过，close 会抛 :class:`WebSocketError`。
    """
    buffer = b""
    opcode = OP_CONT
    while True:
        first = _recv_exact(sock, 2)
        fin = bool(first[0] & 0x80)
        current_op = first[0] & 0x0F
        masked = bool(first[1] & 0x80)
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", _recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
        if length > MAX_FRAME:
            raise WebSocketError(f"帧过大（{length} 字节），中断连接")
        key = _recv_exact(sock, 4) if masked else b""
        payload = _recv_exact(sock, length) if length else b""
        if masked:
            payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))

        if current_op == OP_PING:
            sock.sendall(encode_frame(OP_PONG, payload, mask=False))
            continue
        if current_op == OP_PONG:
            continue
        if current_op == OP_CLOSE:
            raise WebSocketError("对端请求关闭连接")
        if current_op != OP_CONT:
            opcode = current_op
        buffer += payload
        if fin:
            return opcode, buffer


class WebSocketClient:
    """一条 WebSocket 连接，串行发 JSON-RPC 请求。

    线程安全：``call()`` 用锁串行化，因此多个调用方不会交叉读写帧。
    本项目里只会有遥测线程与保活钩子用它，串行足够。
    """

    def __init__(self, url: str, *, api_key: str = "", timeout: float = 6.0) -> None:
        self.url = url
        self.api_key = api_key
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._sequence = 0
        #: 收到的通知（没有 id 的消息），供上层按需读取
        self.notifications: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ 连接
    def connect(self) -> None:
        parsed = urlparse(self.url)
        secure = parsed.scheme in ("wss", "https")
        host = parsed.hostname or ""
        port = parsed.port or (443 if secure else 80)
        path = parsed.path or "/websocket"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        elif self.api_key:
            path = f"{path}?token={self.api_key}"

        raw = socket.create_connection((host, port), timeout=self.timeout)
        if secure:
            context = ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
        )
        if self.api_key:
            request += f"X-Api-Key: {self.api_key}\r\n"
        request += "\r\n"
        raw.sendall(request.encode("ascii"))

        # 读握手响应（到空行为止）
        header = bytearray()
        while b"\r\n\r\n" not in header:
            chunk = raw.recv(1)
            if not chunk:
                raw.close()
                raise WebSocketError("握手期间连接被关闭")
            header += chunk
            if len(header) > 16384:
                raw.close()
                raise WebSocketError("握手响应异常长")
        text = header.decode("latin-1")
        status_line = text.split("\r\n", 1)[0]
        if " 101" not in status_line:
            raw.close()
            raise WebSocketError(f"服务端拒绝升级为 WebSocket：{status_line}")
        # 校验 accept key（RFC 6455 要求）
        expected = base64.b64encode(
            __import__("hashlib").sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if expected.lower() not in text.lower():
            raw.close()
            raise WebSocketError("Sec-WebSocket-Accept 校验失败")
        raw.settimeout(self.timeout)
        self._sock = raw

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.sendall(self._encode_frame(OP_CLOSE, b""))
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # ------------------------------------------------------------------ 帧编解码
    def _encode_frame(self, opcode: int, payload: bytes) -> bytes:
        """客户端发帧（带掩码）。"""
        return encode_frame(opcode, payload, mask=True)

    def _read_frame(self) -> tuple[int, bytes]:
        """读一帧并把分片聚合起来。"""
        if self._sock is None:
            raise WebSocketError("连接未建立")
        return read_frame(self._sock, expect_masked=False)

    # ------------------------------------------------------------------ 调用
    def call(self, method: str, params: Optional[dict] = None, *, wait: float = 6.0) -> Any:
        """发一条 JSON-RPC 请求并等它的结果。

        返回 ``result`` 字段；服务端回 ``error`` 时抛 :class:`WebSocketError`。
        期间收到的通知（没有 id）会存进 ``notifications``。
        """
        with self._lock:
            if self._sock is None:
                self.connect()
            self._sequence += 1
            message_id = self._sequence
            payload = {"jsonrpc": "2.0", "method": method, "id": message_id}
            if params is not None:
                payload["params"] = params
            body = json.dumps(payload).encode("utf-8")
            assert self._sock is not None
            self._sock.sendall(self._encode_frame(OP_TEXT, body))

            deadline = time.time() + wait
            while time.time() < deadline:
                assert self._sock is not None
                self._sock.settimeout(max(0.2, min(self.timeout, deadline - time.time())))
                try:
                    opcode, data = self._read_frame()
                except (socket.timeout, TimeoutError):
                    continue
                except OSError as exc:
                    self.close()
                    raise WebSocketError(f"读取响应失败：{exc}") from exc
                if opcode != OP_TEXT:
                    continue
                try:
                    decoded = json.loads(data.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(decoded, dict):
                    continue
                if decoded.get("id") == message_id:
                    if "error" in decoded:
                        raise WebSocketError(f"{method} 返回错误：{decoded['error']}")
                    return decoded.get("result")
                # 其它消息当作通知收集起来
                self.notifications.append(decoded)
                if len(self.notifications) > 200:
                    del self.notifications[:100]
            raise WebSocketError(f"{method} 等待响应超时")
