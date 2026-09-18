"""X1 / P2S / H2 / X2D 系列的 RTSPS 视频通道（可选，需要 opencv-python）。

地址：`rtsps://bblp:{访问代码}@{IP}:322/streaming/live/1`
部分固件默认关闭该服务，需要在打印机屏幕上打开
「局域网模式实时画面 / LAN Mode Liveview」。A1、P1 系列没有这个接口，
统一走 6000 端口 JPEG 流。
"""

from __future__ import annotations

import importlib.util
import os
import threading
import time
from typing import Callable, Optional
from urllib.parse import quote

from .ports import RTSP_PORT
from .timeouts import RTSP_FIRST_FRAME_TIMEOUT, RTSP_OPEN_TIMEOUT_MS

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
        self._first_frame_event = threading.Event()
        self.state = self.STATE_STOPPED
        self.detail = ""
        self._started_at = 0.0
        self._path_index = 0
        self.active_path = self._paths[0] if self._paths else self.DEFAULT_PATHS[0]

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("cv2") is not None

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
        """等待首帧；一旦确定连不上就提前返回，避免白白等满超时。"""
        deadline = time.time() + timeout
        started = time.time()
        while time.time() < deadline:
            if self._first_frame_event.wait(0.2):
                return self.latest_frame()[1]
            failed = self.state in (
                self.STATE_RETRYING,
                self.STATE_AUTH_ERROR,
                self.STATE_STOPPED,
            )
            if failed and (time.time() - started) > 1.5:
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
        while not self._stop_event.is_set():
            path = self._paths[self._path_index % len(self._paths)]
            self._set_state(
                self.STATE_CONNECTING, f"正在连接 RTSPS {self.host}:{RTSP_PORT}{path}"
            )
            capture = None
            try:
                # 给 FFmpeg 设置打开/读取超时，避免打印机未开启该服务时长时间卡住
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
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        self._set_state(self.STATE_RETRYING, "RTSPS 画面中断，正在重连")
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
            backoff = min(backoff * 1.5, 20.0)
        self._set_state(self.STATE_STOPPED, "已停止")
