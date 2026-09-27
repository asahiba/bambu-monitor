"""X1 / P2S / H2 / X2D 系列的 RTSPS 视频通道（可选，需要 opencv-python）。

地址：`rtsps://bblp:{访问代码}@{IP}:322/streaming/live/1`
部分固件默认关闭该服务，需要在打印机屏幕上打开
「局域网模式实时画面 / LAN Mode Liveview」。A1、P1 系列没有这个接口，
统一走 6000 端口 JPEG 流。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import socket
import ssl
import threading
import time
from typing import Callable, Optional
from urllib.parse import quote

from .framegap import FrameGapTracker
from .ports import RTSP_PORT
from .timeouts import (
    RTSP_FIRST_FRAME_TIMEOUT,
    RTSP_OPEN_TIMEOUT_MS,
    RTSPS_BACKOFF_MAX,
    RTSPS_PREFLIGHT_PAUSE,
    RTSPS_PREFLIGHT_TIMEOUT,
    RTSPS_STALE_READ_SECONDS,
)

LOGGER = logging.getLogger("bambu-monitor.rtsp")

# 自签证书 + 强制 TCP 传输，必须在导入 cv2 之前设置
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|tls_verify;0")
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")


class RtspStream(threading.Thread):
    STATE_CONNECTING = "connecting"
    STATE_STREAMING = "streaming"
    STATE_RETRYING = "retrying"
    STATE_STOPPED = "stopped"
    STATE_AUTH_ERROR = "auth_error"

    def __init__(
        self,
        host: str,
        access_code: str,
        on_frame: Optional[Callable[[bytes], None]] = None,
        on_state: Optional[Callable[[str, str], None]] = None,
        name: str = "",
        max_fps: float = 12.0,
        open_timeout_ms: int = RTSP_OPEN_TIMEOUT_MS,
        paths: Optional[tuple[str, ...]] = None,
    ) -> None:
        super().__init__(name=f"rtsp-{name or host}", daemon=True)
        self.host = host
        self.access_code = access_code
        self._on_frame = on_frame
        self._on_state = on_state
        self._max_fps = max_fps
        self._open_timeout_ms = open_timeout_ms
        self._paths = tuple(paths) if paths else self.DEFAULT_PATHS
        #: 编码目标尺寸：按画面控件大小编码，避免为看不见的像素浪费 CPU
        self._target_size: tuple[int, int] = (640, 360)
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._latest: Optional[bytes] = None
        self._latest_seq = 0
        self._frame_count = 0
        self._last_frame_ts = 0.0
        #: 这台设备自己的出帧节奏（用户把帧率设得很低时，间隔会明显大于 6 秒，
        #: 判「画面还在不在」要按实测节奏来，见 app.bambu.framegap）
        self._gaps = FrameGapTracker()
        self._first_frame_event = threading.Event()
        self.state = self.STATE_STOPPED
        self.detail = ""
        self._started_at = 0.0
        self._path_index = 0
        self.active_path = self._paths[0] if self._paths else self.DEFAULT_PATHS[0]

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("cv2") is not None

    def preflight(self, path: str = "", timeout: float = RTSPS_PREFLIGHT_TIMEOUT) -> tuple[bool, str]:
        """用**纯 Python** 快速探一次「打印机这次会不会理我们」。

        ## 为什么必须有这一步

        真机实测（X2D，2026-09）：322 端口 TCP 永远可连，但 **TLS 握手会不定期地
        完全没有响应** —— 连续握 4 次，2 次超时（TLS 1.2 与 1.3 都会中招，所以不是
        协议版本问题）。也就是说这台设备的实时画面服务大约一半的新连接会石沉大海。

        而 OpenCV / FFmpeg 那条路**拿不到这个控制权**：`CAP_PROP_OPEN_TIMEOUT_MSEC`
        管不到 TLS 握手，一次石沉就能让 `VideoCapture` 卡上十几秒甚至更久，
        表现出来就是用户说的「画面经常卡住」，尤其是帧率高的机型（X2D / H2 / P2S）
        —— 它们的数据量大，连接被挂住的概率也更高。

        所以先用我们自己的 socket + ssl 探一下（超时可控、TLS 版本自动降级），
        拿到 RTSP 的响应行才算「这次能用」，再去开 OpenCV。探不通就等一小会儿重来，
        而不是让 FFmpeg 白等十几秒。

        :returns: ``(是否可用, 说明)``；说明里带响应行或失败原因。
        """
        target = path or self.active_path
        url = self.url(path=target)
        request = (
            f"DESCRIBE {url} RTSP/1.0\r\n"
            f"CSeq: 1\r\n"
            f"Accept: application/sdp\r\n"
            f"User-Agent: BambuMonitor\r\n\r\n"
        ).encode("ascii", "ignore")
        # ⚠️ 这里自己建 socket，**不用 `tlsutil.connect_tls`**：那个函数会把超时换成
        # 它自己的 HANDSHAKE_TIMEOUT、还会按候选参数逐个重试，实测一次要 12 秒 ——
        # 而这一步的全部意义就是"快"。自己用一个上下文、把超时卡死在 timeout 上。
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        tls = None
        try:
            raw = socket.create_connection((self.host, RTSP_PORT), timeout=timeout)
            raw.settimeout(timeout)
            tls = context.wrap_socket(raw, server_hostname=self.host)
            tls.settimeout(timeout)
            tls.sendall(request)
            data = tls.recv(2048).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - 探测失败很常见，交给调用方重试
            return False, f"RTSP 服务没有响应（{type(exc).__name__}: {exc}）"
        finally:
            if tls is not None:
                try:
                    tls.close()
                except OSError:
                    pass
        status = data.splitlines()[0].strip() if data else ""
        if "RTSP/1.0" not in status:
            return False, f"RTSP 响应异常：{status or '（空）'}"
        # 401 也算通：说明服务活着，只是等我们带上 Digest 鉴权（OpenCV 会自己带）
        return True, status

    #: 不同机型/固件的 RTSP 路径可能不同，按顺序尝试
    DEFAULT_PATHS = ("/streaming/live/1", "/streaming/live/2", "/live/1")

    def url(self, scheme: str = "rtsps", path: str = "/streaming/live/1") -> str:
        code = quote(self.access_code, safe="")
        return f"{scheme}://bblp:{code}@{self.host}:{RTSP_PORT}{path}"

    def set_target_size(self, width: int, height: int) -> None:
        """按画面控件的尺寸编码，避免为看不见的像素白白消耗 CPU。"""
        width = max(160, int(width))
        height = max(90, int(height))
        with self._lock:
            if (width, height) != self._target_size:
                self._target_size = (width, height)

    def set_max_fps(self, fps: float) -> None:
        with self._lock:
            self._max_fps = max(0.0, float(fps))

    # ------------------------------------------------------------------ 接口
    def stop(self) -> None:
        self._stop_event.set()

    def latest_frame(self) -> tuple[int, Optional[bytes]]:
        with self._lock:
            return self._latest_seq, self._latest

    def wait_first_frame(self, timeout: float = RTSP_FIRST_FRAME_TIMEOUT) -> Optional[bytes]:
        """等待首帧。

        ⚠️ ``retrying`` **不算失败**：现在开流前会先做纯 Python 预探，打印机不理我们时
        会快速重试（真机上约一半的新连接会石沉），这时候继续等才可能等到首帧。
        只有 ``auth_error``（口令不对）与 ``stopped``（已停）才值得提前放弃 ——
        以前把 ``retrying`` 也算失败，于是首次预探一失败就立刻返回，界面显示没画面，
        而其实再试一次就通了。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._first_frame_event.wait(0.2):
                return self.latest_frame()[1]
            if self.state in (self.STATE_AUTH_ERROR, self.STATE_STOPPED):
                return None
        return None

    @property
    def fps(self) -> float:
        elapsed = time.time() - self._started_at if self._started_at else 0
        return self._frame_count / elapsed if elapsed > 0 else 0.0

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def last_frame_age(self) -> float:
        if self._last_frame_ts <= 0:
            return 1e9
        return time.time() - self._last_frame_ts

    @property
    def frame_gap(self) -> float:
        """最近若干帧里的最大间隔（这台设备自己的节奏，见 :mod:`app.bambu.framegap`）。"""
        return self._gaps.max_gap

    # ------------------------------------------------------------------ 内部
    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        self.detail = detail
        if self._on_state is not None:
            try:
                self._on_state(state, detail)
            except Exception:
                pass

    def run(self) -> None:
        self._started_at = time.time()
        try:
            import cv2  # noqa: PLC0415
        except ImportError:
            self._set_state(self.STATE_STOPPED, "未安装 opencv-python，无法使用 RTSPS")
            return

        backoff = 2.0
        #: 预探连续失败次数：连着探不通就按退避等待，别把打印机的实时画面服务压垮
        preflight_failures = 0
        while not self._stop_event.is_set():
            path = self._paths[self._path_index % len(self._paths)]
            self._set_state(
                self.STATE_CONNECTING, f"正在连接 RTSPS {self.host}:{RTSP_PORT}{path}"
            )
            capture = None
            try:
                # ① 先用纯 Python 探一次（真机上约一半的新连接会石沉，见 preflight 的说明）：
                #    探不通就快速重来，而不是让 OpenCV/FFmpeg 在里面白等十几秒。
                ok, why = self.preflight(path)
                if not ok:
                    preflight_failures += 1
                    self._set_state(self.STATE_RETRYING, why)
                    if preflight_failures >= 3:
                        # 连探 3 次都不理：退到另一条候选路径试试（有的固件路径不同）
                        self._path_index += 1
                    if self._stop_event.wait(
                        RTSPS_PREFLIGHT_PAUSE * min(preflight_failures, 5)
                    ):
                        break
                    continue
                if preflight_failures:
                    LOGGER.info("RTSPS 预探第 %d 次恢复响应（%s）", preflight_failures, why)
                    preflight_failures = 0
                # 预探通过之后才轮到 FFmpeg：它能打开就说明这条路通了
                # （真机上"预探通过、FFmpeg 立刻又失败"也会发生，那就当普通失败重来）
                # ② 给 FFmpeg 设置打开/读取超时，避免打印机未开启该服务时长时间卡住
                params = [
                    int(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC),
                    int(self._open_timeout_ms),
                    int(cv2.CAP_PROP_READ_TIMEOUT_MSEC),
                    int(self._open_timeout_ms),
                ]
                url = self.url(path=path)
                try:
                    capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
                except TypeError:  # 老版本 OpenCV 不支持参数数组
                    capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
                try:
                    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                except Exception:
                    pass
                if not capture.isOpened():
                    # 换下一个候选路径再试
                    self._path_index += 1
                    raise RuntimeError(
                        "无法打开 RTSPS 流（未开启「局域网实时画面」，或访问代码不正确）"
                    )
                # 这一路开起来了：退避立刻回到起点 —— 真机上打印机会不定期拒连接，
                # 若沿用之前累积的大退避，下一轮重连又要多等十几秒（"画面不持久"）。
                backoff = 2.0
                interval = 1.0 / max(1.0, self._max_fps)
                last_emit = 0.0
                while not self._stop_event.is_set():
                    with self._lock:
                        max_fps = self._max_fps
                        target = self._target_size
                    interval = (1.0 / max_fps) if max_fps > 0 else 0.0
                    # 先按帧率节流再读帧：解码才是 CPU 大头，
                    # 「先解码再丢帧」等于白烧 CPU（实测能差出 30% 单核）。
                    if interval:
                        gap = interval - (time.time() - last_emit)
                        if gap > 0:
                            if self._stop_event.wait(min(gap, 0.5)):
                                break
                            continue
                    read_started = time.time()
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        self._set_state(self.STATE_RETRYING, "RTSPS 画面中断，正在重连")
                        break
                    # 读到的帧太旧说明这次连接其实已经废了（FFmpeg 里可能积压了数据）：
                    # 丢掉过期帧，别把几分钟前的画面当"最新"贴到界面上
                    age = time.time() - read_started
                    if age > RTSPS_STALE_READ_SECONDS:
                        self._set_state(
                            self.STATE_RETRYING, f"RTSPS 取帧卡了 {age:.0f} 秒，正在重连"
                        )
                        break
                    last_emit = time.time()
                    # 先缩小再编码：1080p → 画面实际尺寸，CPU 与带宽都省一大截
                    try:
                        height, width = frame.shape[:2]
                        if target and (width > target[0] or height > target[1]):
                            scale = min(target[0] / width, target[1] / height)
                            frame = cv2.resize(
                                frame,
                                (max(2, int(width * scale)), max(2, int(height * scale))),
                                interpolation=cv2.INTER_AREA,
                            )
                    except Exception:
                        pass
                    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                    if not ok:
                        continue
                    data = buffer.tobytes()
                    with self._lock:
                        self._latest = data
                        self._latest_seq += 1
                        self._last_frame_ts = time.time()
                    self._gaps.note(self._last_frame_ts)
                    self._frame_count += 1
                    if not self._first_frame_event.is_set():
                        self._first_frame_event.set()
                        backoff = 2.0
                    self.active_path = path
                    if self.state != self.STATE_STREAMING:
                        self._set_state(self.STATE_STREAMING, f"RTSPS 已连接（{path}）")
                    if self._on_frame is not None:
                        try:
                            self._on_frame(data)
                        except Exception:
                            pass
            except Exception as exc:  # noqa: BLE001 - 任何解码异常都按重连处理
                self._set_state(self.STATE_RETRYING, f"RTSPS 错误：{exc}")
            finally:
                if capture is not None:
                    try:
                        capture.release()
                    except Exception:
                        pass
            if self._stop_event.wait(backoff):
                break
            backoff = min(backoff * 1.5, RTSPS_BACKOFF_MAX)
        self._set_state(self.STATE_STOPPED, "已停止")
