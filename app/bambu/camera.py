"""拓竹 6000 端口摄像头流客户端。

所有机型（A1 / A1 mini / P1P / P1S / X1C / X2D …）都提供同一个本地视频服务：

* 端口 6000，TLS 加密（证书由 Bambu Lab CA 签发）
* 连接后先发送 80 字节鉴权包（用户名 `bblp` + 局域网访问代码）
* 之后服务器按帧发送：16 字节帧头 + 若干字节 JPEG 数据

帧头（小端）：

======  ======  ==========================================
偏移    长度    含义
======  ======  ==========================================
0       4       JPEG 数据长度（不含帧头）
4       4       0（itrack）
8       4       1（flags）
12      4       0
======  ======  ==========================================

鉴权包（小端）：

======  ======  ==========================================
偏移    长度    含义
======  ======  ==========================================
0       4       0x40（内联负载长度）
4       4       0x3000（类型）
8       8       0
16      32      用户名，ASCII，右侧补 \\0
48      32      访问代码，ASCII，右侧补 \\0
======  ======  ==========================================
"""

from __future__ import annotations

import socket
import ssl
import struct
import threading
import time
from typing import Callable, Optional

from . import tlsutil
from .framegap import FrameGapTracker
from .ports import CAMERA_PORT
from .timeouts import (
    CAMERA_AUTH_BACKOFF,
    CAMERA_BACKOFF_FACTOR,
    CAMERA_BACKOFF_MAX,
    CAMERA_BACKOFF_START,
    CAMERA_FIRST_FRAME_TIMEOUT,
    CAMERA_FRAME_BODY_TIMEOUT,
    CAMERA_FRAME_HEADER_TIMEOUT,
    CAMERA_READ_SLICE,
    CAMERA_STREAM_JOIN,
    CAMERA_TLS_TIMEOUT,
)

AUTH_USERNAME = "bblp"
AUTH_TYPE = 0x3000
AUTH_INLINE_SIZE = 0x40
#: 鉴权包固定长度（协议规定）
AUTH_PACKET_SIZE = 80
FRAME_HEADER_SIZE = 16
MAX_FRAME_SIZE = 8 * 1024 * 1024
MIN_FRAME_SIZE = 512
JPEG_SOI = b"\xff\xd8"


def build_auth_packet(access_code: str, username: str = AUTH_USERNAME) -> bytes:
    """构造 80 字节鉴权包。"""
    packet = bytearray()
    packet += struct.pack("<IIII", AUTH_INLINE_SIZE, AUTH_TYPE, 0, 0)
    packet += username.encode("ascii", errors="ignore")[:32].ljust(32, b"\x00")
    packet += access_code.encode("ascii", errors="ignore")[:32].ljust(32, b"\x00")
    # 这里**不能用 assert**：``python -O`` 会把它整条去掉，长度错了就会静默发出
    # 一个畸形的鉴权包（打印机只会断开连接，排查起来毫无线索）。
    if len(packet) != AUTH_PACKET_SIZE:
        raise ValueError(
            f"鉴权包长度必须为 {AUTH_PACKET_SIZE} 字节，实际 {len(packet)} 字节"
        )
    return bytes(packet)


class CameraStream(threading.Thread):
    """后台线程：持续接收 JPEG 帧。

    * ``on_frame(jpeg_bytes)``  收到完整帧时回调
    * ``on_state(state, detail)`` 连接状态变化时回调
    """

    STATE_CONNECTING = "connecting"
    STATE_STREAMING = "streaming"
    STATE_RETRYING = "retrying"
    STATE_STOPPED = "stopped"
    STATE_AUTH_ERROR = "auth_error"

    def __init__(
        self,
        host: str,
        access_code: str,
        serial: str = "",
        port: int = CAMERA_PORT,
        on_frame: Optional[Callable[[bytes], None]] = None,
        on_state: Optional[Callable[[str, str], None]] = None,
        name: str = "",
    ) -> None:
        super().__init__(name=f"camera-{name or host}", daemon=True)
        self.host = host
        self.port = port
        self.access_code = access_code
        self.serial = serial
        self._on_frame = on_frame
        self._on_state = on_state
        self._stop_event = threading.Event()
        self._sock: Optional[ssl.SSLSocket] = None
        self._lock = threading.Lock()
        self._latest: Optional[bytes] = None
        self._latest_seq = 0
        self._frame_count = 0
        self._last_frame_ts = 0.0
        #: 这台设备自己的出帧节奏（6000 端口的 P1/A1 只有 0.2 左右 fps，
        #: 而且间隔很不匀），判「画面还在不在」时要按它来，不能写死秒数。
        self._gaps = FrameGapTracker()
        self._first_frame_event = threading.Event()
        #: 最近一次读是否"对端关闭了连接"（与超时区分，见 `_read_exact`）
        self.peer_closed = False
        self.state = self.STATE_STOPPED
        self.detail = ""
        self.tls_verified = False
        self._started_at = 0.0

    # ------------------------------------------------------------------ 对外接口
    def stop(self) -> None:
        self._stop_event.set()
        self._close_socket()

    def latest_frame(self) -> tuple[int, Optional[bytes]]:
        """返回 (序号, JPEG 数据)，用于界面按需取帧，避免信号队列堆积。"""
        with self._lock:
            return self._latest_seq, self._latest

    def wait_first_frame(
        self, timeout: float = 10.0, should_stop: Optional[Callable[[], bool]] = None
    ) -> Optional[bytes]:
        """等待首帧；确认被拒绝时提前返回，不再空等。

        ``should_stop`` 用于外部取消（例如用户关掉了「测试连接」对话框）：
        一旦返回真值就立刻退出，避免线程被拖到超时 —— 界面线程在等它结束。
        """
        deadline = time.time() + timeout
        started = time.time()
        while time.time() < deadline:
            if self._first_frame_event.wait(0.2):
                return self.latest_frame()[1]
            if should_stop is not None and should_stop():
                return None
            failed = self.state in (self.STATE_AUTH_ERROR, self.STATE_STOPPED)
            if failed and (time.time() - started) > 1.0:
                return None
        return None

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def last_frame_age(self) -> float:
        """距离最近一帧的秒数（用于判断画面是否真的在线）。"""
        if self._last_frame_ts <= 0:
            return 1e9
        return time.time() - self._last_frame_ts

    @property
    def frame_gap(self) -> float:
        """最近若干帧里的最大间隔（这台设备自己的节奏，见 :mod:`app.bambu.framegap`）。"""
        return self._gaps.max_gap

    @property
    def fps(self) -> float:
        elapsed = time.time() - self._started_at if self._started_at else 0
        if elapsed <= 0:
            return 0.0
        return self._frame_count / elapsed

    # ------------------------------------------------------------------ 内部实现
    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        self.detail = detail
        if self._on_state is not None:
            try:
                self._on_state(state, detail)
            except Exception:
                pass

    def _close_socket(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _read_exact(self, count: int, timeout: float = CAMERA_FRAME_BODY_TIMEOUT) -> Optional[bytes]:
        """读取固定长度数据，超时返回 None（不关闭连接，由调用方决定）。

        ⚠️ 同时记下"是**对端关了连接**还是**只是超时**"（见 ``self.peer_closed``）：
        这两种情况在诊断上完全不同 —— 对端在我们发完鉴权包后立刻关闭 = 访问代码不对；
        超时 = 流中断。以前两者都返回 None，调用方只能猜。
        """
        buf = bytearray()
        deadline = time.time() + timeout
        sock = self._sock
        if sock is None:
            return None
        while len(buf) < count:
            if self._stop_event.is_set():
                return None
            remaining = deadline - time.time()
            if remaining <= 0:
                self.peer_closed = False
                return None
            sock.settimeout(min(remaining, CAMERA_READ_SLICE))
            try:
                chunk = sock.recv(count - len(buf))
            except (socket.timeout, TimeoutError):
                continue
            except ssl.SSLWantReadError:
                continue
            except (OSError, ssl.SSLError):
                self.peer_closed = False
                return None
            if not chunk:
                self.peer_closed = True  # 对端关闭
                return None
            buf += chunk
        self.peer_closed = False
        return bytes(buf)

    def _open(self) -> bool:
        self._set_state(self.STATE_CONNECTING, f"正在连接 {self.host}:{self.port}")
        try:
            sock, verified = tlsutil.connect_tls(
                self.host,
                self.port,
                timeout=CAMERA_TLS_TIMEOUT,
                server_hostname=self.serial or self.host,
                verify_chain=True,
            )
        except (OSError, ssl.SSLError) as exc:
            self._set_state(self.STATE_RETRYING, f"连接失败：{exc}")
            return False
        self._sock = sock
        self.tls_verified = verified
        try:
            sock.sendall(build_auth_packet(self.access_code))
        except (OSError, ssl.SSLError) as exc:
            self._set_state(self.STATE_RETRYING, f"鉴权包发送失败：{exc}")
            self._close_socket()
            return False
        return True

    def _pump(self) -> None:
        """读取帧，直到出错或停止。"""
        idle_timeouts = 0
        frames_in_cycle = 0
        while not self._stop_event.is_set():
            header = self._read_exact(FRAME_HEADER_SIZE, timeout=CAMERA_FRAME_HEADER_TIMEOUT)
            if header is None:
                if self._stop_event.is_set():
                    return
                if self.peer_closed and frames_in_cycle == 0:
                    # 发完鉴权包就断开、一帧都没给：这才是"访问代码不对"的真实表现
                    # （真机实测：口令错时打印机直接关连接，不会回任何帧头）。
                    self._set_state(
                        self.STATE_AUTH_ERROR,
                        "打印机拒绝了访问代码（发完鉴权包就被断开）：请核对局域网访问码",
                    )
                    return
                idle_timeouts += 1
                if idle_timeouts >= 2:
                    self._set_state(self.STATE_RETRYING, "视频流中断，正在重连")
                    return
                continue
            idle_timeouts = 0
            payload_size, _itrack, flags, _reserved = struct.unpack_from("<IIII", header, 0)
            if not (MIN_FRAME_SIZE <= payload_size <= MAX_FRAME_SIZE):
                # ⚠️ 2026-10-01 真机教训：**小包不等于访问代码错**。
                # X1C / X2D 在 6000 端口上都回一个 8 字节包
                # （帧头 flags=0、负载 `FF FF FF FF 00 00 00 00`）——那表示
                # "这个端口不提供画面"，而它们的访问代码是对的（MQTT 都连着）。
                # 老代码把它当"口令错"，于是界面上一直挂着「访问代码错误」，
                # 用户跑去改一个本来就正确的口令，还让看门狗在两通道之间来回跳。
                # 正常帧的 flags 按协议固定为 1（见模块开头的帧头说明）。
                if flags != 1:
                    self._set_state(
                        self.STATE_RETRYING,
                        "这台打印机不通过 6000 端口提供画面（返回空帧），稍后重试",
                    )
                else:
                    self._set_state(
                        self.STATE_AUTH_ERROR,
                        "打印机拒绝连接：请检查访问代码（局域网访问码）是否正确",
                    )
                return
            payload = self._read_exact(payload_size, timeout=CAMERA_FRAME_BODY_TIMEOUT)
            if payload is None:
                if self._stop_event.is_set():
                    return
                self._set_state(self.STATE_RETRYING, "图像数据接收超时")
                return
            if not payload.startswith(JPEG_SOI):
                # 极少数固件会多带 4 字节，做一次容错
                index = payload.find(JPEG_SOI)
                if index > 0:
                    payload = payload[index:]
                else:
                    continue
            with self._lock:
                self._latest = payload
                self._latest_seq += 1
                self._last_frame_ts = time.time()
            self._gaps.note(self._last_frame_ts)
            self._frame_count += 1
            frames_in_cycle += 1
            if not self._first_frame_event.is_set():
                self._first_frame_event.set()
            # 断线重连后也要重新上报「已连接」，否则状态会一直停在「正在连接」
            if self.state != self.STATE_STREAMING:
                self._set_state(self.STATE_STREAMING, "视频已连接")
            if self._on_frame is not None:
                try:
                    self._on_frame(payload)
                except Exception:
                    pass

    def run(self) -> None:
        self._started_at = time.time()
        backoff = CAMERA_BACKOFF_START
        while not self._stop_event.is_set():
            if not self._open():
                if self._stop_event.wait(backoff):
                    break
                backoff = min(backoff * CAMERA_BACKOFF_FACTOR, CAMERA_BACKOFF_MAX)
                continue
            backoff = CAMERA_BACKOFF_START
            self._pump()
            self._close_socket()
            if self._stop_event.is_set():
                break
            if self.state == self.STATE_AUTH_ERROR:
                # 访问代码错误时不必高频重试（用户改完代码会走 restart() 立刻重建）
                if self._stop_event.wait(CAMERA_AUTH_BACKOFF):
                    break
            elif self._stop_event.wait(1.0):
                break
        self._close_socket()
        self._set_state(self.STATE_STOPPED, "已停止")


def grab_single_frame(
    host: str,
    access_code: str,
    serial: str = "",
    timeout: float = CAMERA_FIRST_FRAME_TIMEOUT,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Optional[bytes]:
    """同步抓取一帧（用于「测试连接」与自动化测试）。

    ``should_stop`` 可让调用方中途取消（对话框被关闭时用得上）。
    """
    stream = CameraStream(host, access_code, serial=serial)
    stream.start()
    try:
        frame = stream.wait_first_frame(timeout, should_stop=should_stop)
    finally:
        stream.stop()
        stream.join(timeout=CAMERA_STREAM_JOIN)
    return frame
