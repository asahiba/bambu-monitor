"""纯 Python 的 RTSPS(322) 客户端：只做「取流」，不做解码。

## 它解决什么问题

只提供 RTSPS 通道的机型（X1 / X1C / X2D / H2 / P2S…）在**安卓版**上看不到画面：
APK 刻意不打包 OpenCV（Chaquopy 仓库里的 numpy/opencv 轮子是 **4096 字节对齐**的，
在 16KB 内存页设备上会被系统拒绝加载 → 一导入就闪退；实测 `p_align=0x1000`
见 `docs/PACKAGING.md`）。而 `app/bambu/rtsp.py` 那条路完全依赖
``cv2.VideoCapture`` + FFmpeg 解码，没有 cv2 就直接停摆。

于是这里换一个思路：**不在这边解码**。把 RTSPS 里的 H.264 码流原样取出来
（RTSP + Digest 鉴权 + TCP interleaved RTP + RFC 6184 解包），交给**本来就有硬件
解码器**的一方去解 —— 也就是网页端（Android WebView 的 WebCodecs `VideoDecoder`，
或桌面浏览器）。本模块只依赖标准库（socket / ssl / hashlib / base64），
任何平台都能跑。

## 协议要点（都在真机上验证过，见 docs/FIELD_NOTES.md）

* TLS：322 端口，自签证书 → 不校验证书链（与 6000 端口同理）；
* 鉴权：**Digest**（realm 来自 LIVE555）。注意 Basic 会被拒（401）——
  打印机回的质询形如
  ``WWW-Authenticate: Digest realm="LIVE555 Streaming Media", nonce="..."``；
* 通道：``SETUP ... Transport: RTP/AVP/TCP;unicast;interleaved=0-1``，
  之后 RTP 直接以 ``$<通道><长度><包>`` 的格式混在同一条 TCP 连接里；
* 编码：H.264，``packetization-mode=1``（分片 FU-A），RTP 时间戳 90kHz；
* SPS/PPS 既可能出现在 SDP 的 ``sprop-parameter-sets``，也可能随流内联
  （关键帧前）—— 两处都要收，交给 WebCodecs 的 ``description`` 用。

## 与 rtsp.py 的关系

两者**不是替代关系**：有 OpenCV 时仍然走 ``rtsp.py``（能直接拿到 JPEG 帧，
桌面版每个画面控件都用得上）；没有 OpenCV 时才用本模块（安卓、极简容器）。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import tlsutil
from .ports import RTSP_PORT
from .timeouts import RTSP_FIRST_FRAME_TIMEOUT, RTSP_OPEN_TIMEOUT_MS

LOGGER = logging.getLogger("bambu-monitor.rtsp-h264")

#: 与 CameraStream / RtspStream 保持同一套状态字符串（共用 camera_status_text 的判据）
STATE_CONNECTING = "connecting"
STATE_STREAMING = "streaming"
STATE_RETRYING = "retrying"
STATE_STOPPED = "stopped"
STATE_AUTH_ERROR = "auth_error"

#: 候选路径（不同固件不一样，与 rtsp.py 保持一致）
DEFAULT_PATHS: tuple[str, ...] = ("/streaming/live/1", "/streaming/live/2", "/live/1")

#: RTP 里的 H.264 负载类型（SDP `a=rtpmap:96 H264/90000` 实测就是 96）
DEFAULT_PAYLOAD_TYPE = 96
#: 时钟频率（H.264 固定 90kHz）
CLOCK_RATE = 90_000
#: NAL 类型
NAL_SLICE = 1
NAL_IDR = 5
NAL_SEI = 6
NAL_SPS = 7
NAL_PPS = 8
NAL_AUD = 9
NAL_FU_A = 28
NAL_STAP_A = 24


@dataclass
class AccessUnit:
    """一个访问单元（一帧），**AVCC 格式**：每个 NAL 前 4 字节大端长度。

    WebCodecs 的 ``EncodedVideoChunk`` 要的正是这种格式（配 ``description``）。
    """

    data: bytes
    is_keyframe: bool
    timestamp_us: int
    sequence: int = 0

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class StreamParameters:
    """解码初始化参数（来自 SDP 或流内联）。"""

    sps: bytes = b""
    pps: bytes = b""
    payload_type: int = DEFAULT_PAYLOAD_TYPE
    packetization_mode: int = 1
    clock_rate: int = CLOCK_RATE
    control: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return bool(self.sps and self.pps)

    @property
    def codec_string(self) -> str:
        """WebCodecs / MSE 用的 codec 串，例如 ``avc1.641029``。

        由 SPS 的 profile_idc / constraint flags / level_idc 三段十六进制拼成；
        缺失时退回一个保守的 baseline（解码器通常会据此挑选实现）。
        """
        if len(self.sps) >= 4:
            return "avc1." + self.sps[1:4].hex()
        return "avc1.42E01E"

    @property
    def avcc_description(self) -> bytes:
        """AVCDecoderConfigurationRecord（WebCodecs 的 ``description``）。

        结构（ISO/IEC 14496-15 §5.2.4.1.1）：
        configurationVersion(1) profile(1) compatibility(1) level(1)
        lengthSizeMinusOne(1|0xFC) numOfSPS(1|0xE0) [len(2) sps…] numOfPPS(1) [len(2) pps…]
        """
        if not self.ready:
            return b""
        sps = self.sps
        record = bytearray()
        record += b"\x01"
        record += sps[1:4] if len(sps) >= 4 else b"\x42\xE0\x1E"
        record += b"\xfc"  # lengthSizeMinusOne = 3（4 字节长度前缀）
        record += b"\xe1"  # numOfSequenceParameterSets = 1
        record += len(sps).to_bytes(2, "big")
        record += sps
        record += b"\x01"  # numOfPictureParameterSets
        record += len(self.pps).to_bytes(2, "big")
        record += self.pps
        return bytes(record)


def parse_digest_challenge(header: str) -> dict[str, str]:
    """解析 ``WWW-Authenticate: Digest realm="…", nonce="…"`` 里的字段。"""
    return {
        key.lower(): (quoted or bare)
        for key, quoted, bare in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', header)
    }


def build_digest_header(
    *, username: str, password: str, method: str, uri: str, challenge: dict[str, str]
) -> str:
    """按 RFC 2617 组装 Digest 认证头（LIVE555 不带 qop，这里只实现这一种）。"""

    def md5(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    ha1 = md5(f"{username}:{realm}:{password}")
    ha2 = md5(f"{method}:{uri}")
    response = md5(f"{ha1}:{nonce}:{ha2}")
    parts = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f'response="{response}"',
    ]
    if challenge.get("opaque"):
        parts.append(f'opaque="{challenge["opaque"]}"')
    return "Digest " + ", ".join(parts)


def parse_sdp(sdp: str) -> StreamParameters:
    """从 SDP 里取出解码参数（编码、包化模式、SPS/PPS、媒体级 control）。

    ⚠️ **必须取媒体级（``m=video`` 那一段里）的 ``a=control``**，不能只取会话级的：
    LIVE555 的会话级 control 是 ``*``（语义是"用请求的 URL 本身"），
    拿它拼成 SETUP 目标会得到 ``405 Method Not Allowed``（实测踩过）。
    媒体级通常是 ``track1`` 这样的相对值，要拼在请求 URL 后面 —— 见 :func:`resolve_control`。
    """
    params = StreamParameters()
    payload = re.search(r"a=rtpmap:(\d+)\s+(\S+)/(\d+)", sdp)
    if payload:
        params.payload_type = int(payload.group(1))
        params.clock_rate = int(payload.group(3))
    mode = re.search(r"packetization-mode=(\d+)", sdp)
    if mode:
        params.packetization_mode = int(mode.group(1))
    sprop = re.search(r"sprop-parameter-sets=([^;\r\n]+)", sdp)
    if sprop:
        parts = [item.strip() for item in sprop.group(1).split(",") if item.strip()]
        if parts:
            try:
                params.sps = base64.b64decode(parts[0])
            except Exception:  # noqa: BLE001 - SDP 里字段坏了不该让整条通道挂掉
                params.sps = b""
        if len(parts) > 1:
            try:
                params.pps = base64.b64decode(parts[1])
            except Exception:  # noqa: BLE001
                params.pps = b""
    session_control, media_control = _session_and_media_control(sdp)
    params.control = media_control or session_control
    params.extra = {"session_control": session_control, "media_control": media_control}
    return params


def _session_and_media_control(sdp: str) -> tuple[str, str]:
    """分别取会话级与媒体级（``m=video`` 段内）的 ``a=control``。"""
    session_control = ""
    media_control = ""
    in_media = False
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("m="):
            in_media = line.startswith("m=video")
            continue
        if not line.startswith("a=control:"):
            continue
        value = line[len("a=control:") :].strip()
        if in_media:
            media_control = value
        elif not session_control:
            session_control = value
    return session_control, media_control


def resolve_control(control: str, presentation_url: str) -> str:
    """把 SDP 里的 control 解析成 SETUP 的目标 URL。

    * 空 / ``*`` → 用呈现 URL 本身（LIVE555 的会话级语义）；
    * ``rtsp://`` / ``rtsps://`` 开头 → 原样用；
    * 其它相对值（``track1``、``streaming/live/1/video``）→ 拼在呈现 URL 后面。
    """
    control = (control or "").strip()
    if not control or control == "*":
        return presentation_url
    if control.startswith(("rtsp://", "rtsps://")):
        return control
    base = presentation_url.rstrip("/")
    suffix = control if control.startswith("/") else f"/{control}"
    return base + suffix


class RtspH264Client(threading.Thread):
    """后台线程：连上 RTSPS，把 H.264 访问单元按 AVCC 交给回调。

    ``on_access_unit(unit)`` 在接收线程里被调用，必须**很快返回**（不要做解码/UI）。
    """

    #: 与 CameraStream / RtspStream 同名同值（界面文案共用一套判据，见 camera_status_text）
    STATE_CONNECTING = STATE_CONNECTING
    STATE_STREAMING = STATE_STREAMING
    STATE_RETRYING = STATE_RETRYING
    STATE_STOPPED = STATE_STOPPED
    STATE_AUTH_ERROR = STATE_AUTH_ERROR

    def __init__(
        self,
        host: str,
        access_code: str,
        on_access_unit: Optional[Callable[[AccessUnit], None]] = None,
        on_state: Optional[Callable[[str, str], None]] = None,
        name: str = "",
        paths: Optional[tuple[str, ...]] = None,
        open_timeout_ms: int = RTSP_OPEN_TIMEOUT_MS,
    ) -> None:
        super().__init__(name=f"rtsp-h264-{name or host}", daemon=True)
        self.host = host
        self.access_code = access_code
        self._on_access_unit = on_access_unit
        self._on_state = on_state
        self._paths = tuple(paths) if paths else DEFAULT_PATHS
        self._open_timeout = max(2.0, open_timeout_ms / 1000.0)
        self.parameters = StreamParameters()
        self.state = STATE_STOPPED
        self.detail = ""
        self._stop_event = threading.Event()
        self._first_unit_event = threading.Event()
        self._sock: Optional[ssl.SSLSocket] = None
        self._session_id = ""
        self._path_index = 0
        self.active_path = self._paths[0]
        self._frame_count = 0
        self._sequence = 0
        self._started_at = 0.0
        self._last_unit_ts = 0.0
        self._rtp_timestamp_base: Optional[int] = None
        #: 收发共用的字节缓冲：RTSP 响应与 interleaved RTP 混在同一条连接里
        self._buffer = bytearray()
        #: 等 RTSP 响应期间顺带收到的 RTP 包（实机确实会先来一个 RTCP 再回响应）
        self._pending_rtp: list[bytes] = []
        #: 当前正在拼装的访问单元
        self._au_nals: list[bytes] = []
        self._au_key = False
        self._au_timestamp: Optional[int] = None
        self._fu_buffer = bytearray()
        self._fu_type = 0
        self._idle_since = 0.0
        #: 收包统计（诊断用）
        self.packets = 0
        self.bytes_in = 0

    # ------------------------------------------------------------------ 对外
    @staticmethod
    def available() -> bool:
        """本模块只用标准库，永远可用（与 rtsp.py 的 cv2 判别区分开）。"""
        return True

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def fps(self) -> float:
        elapsed = time.time() - self._started_at if self._started_at else 0.0
        return self._frame_count / elapsed if elapsed > 0 else 0.0

    @property
    def last_frame_age(self) -> float:
        return time.time() - self._last_unit_ts if self._last_unit_ts else 1e9

    def stop(self) -> None:
        self._stop_event.set()
        self._close_socket()

    def wait_first_unit(self, timeout: float = RTSP_FIRST_FRAME_TIMEOUT) -> bool:
        """等待第一帧（或判定连不上）。返回是否拿到了帧。"""
        deadline = time.time() + timeout
        started = time.time()
        while time.time() < deadline:
            if self._first_unit_event.wait(0.2):
                return True
            if self.state in (STATE_AUTH_ERROR, STATE_STOPPED) and time.time() - started > 1.0:
                return False
            if self.state == STATE_RETRYING and time.time() - started > 3.0:
                return False
        return self._first_unit_event.is_set()

    # ------------------------------------------------------------------ 内部
    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        self.detail = detail
        if self._on_state is not None:
            try:
                self._on_state(state, detail)
            except Exception:  # noqa: BLE001 - 回调失败不该拖垮收流线程
                LOGGER.debug("状态回调失败", exc_info=True)

    def _close_socket(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        # 换连接时缓冲要清空，否则上一轮的半截帧会污染新一轮
        self._buffer.clear()
        self._pending_rtp.clear()

    def run(self) -> None:
        self._started_at = time.time()
        backoff = 2.0
        while not self._stop_event.is_set():
            try:
                if self._session_loop():
                    backoff = 2.0
                else:
                    if self._stop_event.wait(backoff):
                        break
                    backoff = min(backoff * 1.6, 15.0)
            except Exception as exc:  # noqa: BLE001 - 收流线程绝不能因异常退出
                LOGGER.warning("RTSPS(H.264) 通道异常：%s", exc, exc_info=True)
                self._set_state(STATE_RETRYING, f"通道异常：{type(exc).__name__}: {exc}")
                if self._stop_event.wait(backoff):
                    break
        self._close_socket()
        self._set_state(STATE_STOPPED, "已停止")

    def _session_loop(self) -> bool:
        """一次完整的「连接 → 取流」，返回是否需要立刻重连。"""
        path = self._paths[self._path_index % len(self._paths)]
        self.active_path = path
        url = f"rtsps://{self.host}:{RTSP_PORT}{path}"
        self._set_state(STATE_CONNECTING, f"正在连接 RTSPS(H.264) {self.host}:{RTSP_PORT}{path}")
        try:
            sock, _verified = tlsutil.connect_tls(
                self.host, RTSP_PORT, timeout=self._open_timeout, server_hostname=self.host
            )
        except (OSError, ssl.SSLError) as exc:
            self._path_index += 1
            self._set_state(STATE_RETRYING, f"连接失败：{exc}")
            return False
        self._sock = sock
        try:
            cseq = 1
            sdp = ""
            challenge: dict[str, str] = {}
            for attempt_path in (path, *[p for p in self._paths if p != path]):
                sdp, challenge, cseq = self._describe(sock, attempt_path, cseq)
                if sdp:
                    path = attempt_path
                    url = f"rtsps://{self.host}:{RTSP_PORT}{path}"
                    break
                if not challenge:
                    break
            if not sdp:
                if challenge:
                    self._set_state(
                        STATE_AUTH_ERROR,
                        "访问代码错误或未被接受（RTSPS 需要 Digest 鉴权）",
                    )
                else:
                    self._path_index += 1
                    self._set_state(
                        STATE_RETRYING,
                        "无法打开 RTSPS 流（未开启「局域网实时画面」，或访问代码不正确）",
                    )
                return False

            self.parameters = parse_sdp(sdp)
            if self.parameters.sps and self.parameters.pps:
                LOGGER.info(
                    "RTSPS 参数就绪：%s，SPS %d 字节 / PPS %d 字节",
                    self.parameters.codec_string,
                    len(self.parameters.sps),
                    len(self.parameters.pps),
                )

            transport_path = resolve_control(self.parameters.control, url)
            ok, self._session_id, cseq, auth = self._setup(sock, transport_path, challenge, cseq)
            if not ok:
                self._set_state(STATE_RETRYING, "SETUP 失败（打印机拒绝了 TCP interleaved 通道）")
                return False
            if not self._play(sock, url, self._session_id, challenge, cseq):
                self._set_state(STATE_RETRYING, "PLAY 失败")
                return False

            self._set_state(STATE_STREAMING, "RTSPS(H.264) 已连接")
            return self._receive_loop(sock)
        finally:
            self._close_socket()

    def _send(self, sock: ssl.SSLSocket, lines: list[str]) -> None:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))

    def _extract_frames(self) -> None:
        """从缓冲区头部取出完整的 interleaved 帧（RTP 排队，RTCP 丢弃）。

        帧格式：``$`` + 通道号(1) + 长度(2, 大端) + 负载。通道 0 是 RTP、1 是 RTCP。
        """
        while len(self._buffer) >= 4 and self._buffer[0] == 0x24:
            length = int.from_bytes(self._buffer[2:4], "big")
            if length <= 0:
                del self._buffer[:4]
                continue
            if len(self._buffer) < 4 + length:
                return  # 还没收全，等下一次 recv
            channel = self._buffer[1]
            payload = bytes(self._buffer[4 : 4 + length])
            del self._buffer[: 4 + length]
            if channel == 0 and payload:
                self._pending_rtp.append(payload)

    def _read_response(self, sock: ssl.SSLSocket) -> str:
        """读一条完整的 RTSP 响应（**含实体**）；途中遇到的 interleaved 包不能丢。

        两个实机踩过的坑：

        1. 打印机在 PLAY 的响应**之前**先发了一个 interleaved RTCP 包，于是
           "读到空行为止"会先把二进制当成响应头（判成 PLAY 失败，然后无限重连）；
        2. 只看 ``\\r\\n\\r\\n`` 会**只拿到响应头**，而 **SDP 在实体里**（``Content-Length: 497``）
           —— 结果是"DESCRIBE 成功但 sdp 为空"，代码转去试下一个路径、全部 404，
           最后报成"访问代码错误"。所以必须按 ``Content-Length`` 把实体读完。
        """
        sock.settimeout(self._open_timeout)
        while True:
            self._extract_frames()
            if not (self._buffer and self._buffer[0] == 0x24):
                marker = self._buffer.find(b"\r\n\r\n")
                if marker >= 0:
                    header = bytes(self._buffer[: marker + 4]).decode("utf-8", "replace")
                    del self._buffer[: marker + 4]
                    body = self._read_body(sock, header)
                    self._extract_frames()  # 实体后面可能紧跟 RTP
                    return header + body
            chunk = self._recv_chunk(sock)
            if not chunk:
                return ""
            self._buffer += chunk

    def _read_body(self, sock: ssl.SSLSocket, header: str) -> str:
        """按 ``Content-Length`` 读实体（没有该头就当作无实体）。"""
        match = re.search(r"(?im)^content-length:\s*(\d+)", header)
        if not match:
            return ""
        need = int(match.group(1))
        while len(self._buffer) < need:
            chunk = self._recv_chunk(sock)
            if not chunk:
                break
            self._buffer += chunk
        body = bytes(self._buffer[:need]).decode("utf-8", "replace")
        del self._buffer[:need]
        return body

    def _recv_chunk(self, sock: ssl.SSLSocket) -> bytes:
        try:
            return sock.recv(8192)
        except (socket.timeout, TimeoutError):
            return b""
        except ssl.SSLError as exc:
            LOGGER.debug("读响应失败：%s", exc)
            return b""

    def _describe(
        self, sock: ssl.SSLSocket, path: str, cseq: int
    ) -> tuple[str, dict[str, str], int]:
        """DESCRIBE；需要时用 Digest 重试一次。返回 (SDP, 质询字段, 下一个 cseq)。"""
        url = f"rtsps://{self.host}:{RTSP_PORT}{path}"
        cseq += 1
        self._send(
            sock,
            [f"DESCRIBE {url} RTSP/1.0", f"CSeq: {cseq}", "Accept: application/sdp",
             "User-Agent: bambu-monitor"],
        )
        response = self._read_response(sock)
        if response.startswith("RTSP/1.0 200") or " 200 " in response.splitlines()[0]:
            self._debug("DESCRIBE", url, response)
            return self._body(response), {}, cseq

        challenge_line = next(
            (line for line in response.splitlines() if line.lower().startswith("www-authenticate")),
            "",
        )
        if not challenge_line:
            self._debug("DESCRIBE(无质询)", url, response)
            return "", {}, cseq
        challenge = parse_digest_challenge(challenge_line)
        cseq += 1
        header = build_digest_header(
            username="bblp",
            password=self.access_code,
            method="DESCRIBE",
            uri=url,
            challenge=challenge,
        )
        self._send(
            sock,
            [f"DESCRIBE {url} RTSP/1.0", f"CSeq: {cseq}", "Accept: application/sdp",
             "User-Agent: bambu-monitor", f"Authorization: {header}"],
        )
        response = self._read_response(sock)
        self._debug("DESCRIBE(Digest)", url, response)
        if " 200 " in response.splitlines()[0]:
            return self._body(response), challenge, cseq
        return "", challenge, cseq

    @staticmethod
    def _body(response: str) -> str:
        _, _, body = response.partition("\r\n\r\n")
        return body

    def _setup(
        self,
        sock: ssl.SSLSocket,
        transport_path: str,
        challenge: dict[str, str],
        cseq: int,
    ) -> tuple[bool, str, int, str]:
        """SETUP：要求把 RTP 混在同一条 TCP 连接里（interleaved 0-1）。"""
        cseq += 1
        header = build_digest_header(
            username="bblp",
            password=self.access_code,
            method="SETUP",
            uri=transport_path,
            challenge=challenge,
        )
        self._send(
            sock,
            [
                f"SETUP {transport_path} RTSP/1.0",
                f"CSeq: {cseq}",
                f"Authorization: {header}",
                "Transport: RTP/AVP/TCP;unicast;interleaved=0-1",
                "User-Agent: bambu-monitor",
            ],
        )
        response = self._read_response(sock)
        head = response.splitlines()[0] if response else ""
        self._debug("SETUP", transport_path, response)
        session = ""
        match = re.search(r"Session:\s*([^;\r\n]+)", response)
        if match:
            session = match.group(1).strip()
        return (" 200 " in head), session, cseq, header

    @staticmethod
    def _debug(step: str, target: str, response: str) -> None:
        """把 RTSP 交互打出来（``BAMBU_RTSP_DEBUG=1`` 时）。

        排查 322 端口时最有用：401/405/454 之类的差别只有响应头里看得出来。
        响应里混进二进制（RTP）时也直接看到 —— 那说明服务器已经开流了。
        """
        if not os.environ.get("BAMBU_RTSP_DEBUG"):
            return
        head = (response.splitlines() or [""])[0]
        printable = head.isprintable() and head.startswith("RTSP/")
        print(f"  [debug] {step} {target} -> {head if printable else repr(response[:80])}")
        if printable:
            for line in response.splitlines()[1:8]:
                if line.strip():
                    print(f"          {line.strip()[:120]}")

    def _play(
        self,
        sock: ssl.SSLSocket,
        url: str,
        session_id: str,
        challenge: dict[str, str],
        cseq: int,
    ) -> bool:
        cseq += 1
        header = build_digest_header(
            username="bblp",
            password=self.access_code,
            method="PLAY",
            uri=url,
            challenge=challenge,
        )
        lines = [
            f"PLAY {url} RTSP/1.0",
            f"CSeq: {cseq}",
            f"Authorization: {header}",
            "Range: npt=0.000-",
            "User-Agent: bambu-monitor",
        ]
        if session_id:
            lines.append(f"Session: {session_id}")
        self._send(sock, lines)
        response = self._read_response(sock)
        head = response.splitlines()[0] if response else ""
        self._debug("PLAY", url, response)
        return " 200 " in head

    # ------------------------------------------------------------------ 收流
    def _receive_loop(self, sock: ssl.SSLSocket) -> bool:
        """读 interleaved 帧 → 还原 H.264 访问单元。返回是否需要重连。"""
        sock.settimeout(2.0)
        self._idle_since = time.time()

        while not self._stop_event.is_set():
            # 1) 先把排队中的 RTP 处理掉（等响应时顺带收到的也在这里）
            handled = False
            while self._pending_rtp:
                self._handle_rtp(self._pending_rtp.pop(0))
                handled = True
            # 2) 再从缓冲区里取完整帧
            before = len(self._pending_rtp)
            self._extract_frames()
            if len(self._pending_rtp) > before:
                handled = True
            if handled:
                continue
            # 3) 缓冲区里还有半截帧就先不做 recv（等它收全）
            try:
                chunk = sock.recv(8192)
            except (socket.timeout, TimeoutError):
                idle = time.time() - self._idle_since
                if self._au_nals and idle > 2.0:
                    # marker 丢了也不至于把一帧永远攥在手里
                    self._flush_au()
                if idle > 15.0:
                    self._set_state(STATE_RETRYING, "RTSPS 长时间没有数据，正在重连")
                    return False
                continue
            except (OSError, ssl.SSLError) as exc:
                if self._stop_event.is_set():
                    # 主动停止时 socket 是被我们自己关掉的，别报成"连接中断"
                    return False
                self._set_state(STATE_RETRYING, f"连接中断：{exc}")
                return False
            if not chunk:
                self._set_state(STATE_RETRYING, "连接被打印机关闭，正在重连")
                return False
            self._buffer += chunk
            self._idle_since = time.time()
        return False

    def _handle_rtp(self, packet: bytes) -> None:
        """把一个 RTP 包拼进当前访问单元（RFC 6184 解包）。"""
        self.packets += 1
        self.bytes_in += len(packet)
        parsed = self._parse_rtp(packet)
        if parsed is None:
            return
        timestamp, marker, payload = parsed
        if not payload:
            return
        nal_type = payload[0] & 0x1F

        # 帧边界：RTP 时间戳变了 → 上一个访问单元结束
        if self._au_timestamp is not None and timestamp != self._au_timestamp:
            self._flush_au()
        if self._au_timestamp != timestamp:
            self._au_timestamp = timestamp

        if nal_type in (NAL_SPS, NAL_PPS):
            self._remember_parameter_set(nal_type, payload)
            self._au_nals.append(payload)
            if nal_type == NAL_SPS:
                self._au_key = True
        elif nal_type == NAL_FU_A:
            if len(payload) < 2:
                return
            fu_header = payload[1]
            start = bool(fu_header & 0x80)
            end = bool(fu_header & 0x40)
            inner_type = fu_header & 0x1F
            if start:
                self._fu_buffer = bytearray()
                self._fu_type = inner_type
                self._fu_buffer.append(0x60 | inner_type)  # 还原成单 NAL 头
                self._fu_buffer += payload[2:]
            elif self._fu_buffer and inner_type == self._fu_type:
                self._fu_buffer += payload[2:]
            if end and self._fu_buffer:
                nal = bytes(self._fu_buffer)
                self._au_nals.append(nal)
                if self._fu_type == NAL_IDR:
                    self._au_key = True
                self._fu_buffer = bytearray()
        elif nal_type == NAL_STAP_A:
            index = 1
            while index + 2 <= len(payload):
                size = int.from_bytes(payload[index : index + 2], "big")
                index += 2
                if size <= 0 or index + size > len(payload):
                    break
                nal = payload[index : index + size]
                index += size
                if not nal:
                    continue
                inner = nal[0] & 0x1F
                if inner in (NAL_SPS, NAL_PPS):
                    self._remember_parameter_set(inner, nal)
                self._au_nals.append(nal)
                if inner == NAL_IDR:
                    self._au_key = True
        elif nal_type in (NAL_SLICE, NAL_IDR, NAL_SEI):
            self._au_nals.append(payload)
            if nal_type == NAL_IDR:
                self._au_key = True
        # 其它类型（AUD / 未定义）忽略

        if marker:
            self._flush_au()

    def _flush_au(self) -> None:
        """把当前拼好的访问单元交出去。"""
        nals, self._au_nals = self._au_nals, []
        key, self._au_key = self._au_key, False
        if nals:
            self._emit(nals, key)

    @staticmethod
    def _parse_rtp(packet: bytes) -> Optional[tuple[int, bool, bytes]]:
        """解析 RTP 头，返回 (时间戳, marker, 负载)。"""
        if len(packet) < 12:
            return None
        first = packet[0]
        version = first >> 6
        if version != 2:
            return None
        padding = bool(first & 0x20)
        has_extension = bool(first & 0x10)
        csrc_count = first & 0x0F
        second = packet[1]
        marker = bool(second & 0x80)
        timestamp = int.from_bytes(packet[4:8], "big")
        offset = 12 + csrc_count * 4
        if has_extension:
            if len(packet) < offset + 4:
                return None
            ext_words = int.from_bytes(packet[offset + 2 : offset + 4], "big")
            offset += 4 + ext_words * 4
        if len(packet) <= offset:
            return None
        payload = packet[offset:]
        if padding and payload:
            pad = payload[-1]
            if 0 < pad <= len(payload):
                payload = payload[:-pad]
        return timestamp, marker, payload

    def _remember_parameter_set(self, nal_type: int, nal: bytes) -> None:
        if nal_type == NAL_SPS and nal != self.parameters.sps:
            self.parameters.sps = nal
            LOGGER.debug("收到 SPS（%d 字节）", len(nal))
        elif nal_type == NAL_PPS and nal != self.parameters.pps:
            self.parameters.pps = nal
            LOGGER.debug("收到 PPS（%d 字节）", len(nal))

    def _emit(self, nals: list[bytes], is_keyframe: bool) -> None:
        """把一个访问单元按 AVCC 组装并交给回调。"""
        if not nals:
            return
        blob = bytearray()
        for nal in nals:
            blob += len(nal).to_bytes(4, "big")
            blob += nal
        self._sequence += 1
        unit = AccessUnit(
            data=bytes(blob),
            is_keyframe=is_keyframe,
            timestamp_us=self._timestamp_us(),
            sequence=self._sequence,
        )
        self._frame_count += 1
        self._last_unit_ts = time.time()
        if not self._first_unit_event.is_set():
            self._first_unit_event.set()
        if self._on_access_unit is not None:
            try:
                self._on_access_unit(unit)
            except Exception:  # noqa: BLE001
                LOGGER.debug("访问单元回调失败", exc_info=True)

    def _timestamp_us(self) -> int:
        """单调递增的时间戳（微秒）。WebCodecs 只要求单调，不要求与源一致。"""
        return int((time.time() - self._started_at) * 1_000_000)
