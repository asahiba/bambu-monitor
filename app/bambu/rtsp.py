"""X1 / P2S / H2 / X2D 系列的 RTSPS 视频通道（可选，需要 opencv-python）。

地址：`rtsps://bblp:{访问代码}@{IP}:322/streaming/live/1`
部分固件默认关闭该服务，需要在打印机屏幕上打开
「局域网模式实时画面 / LAN Mode Liveview」。A1、P1 系列没有这个接口，
统一走 6000 端口 JPEG 流。

## 真机约束：一台打印机同时只伺候一个画面客户端（2026-09-28 实测）

同一台 X1C（322 端口 720p）：

* 我们的程序连着这台机器时，另一个客户端（本仓库的 `tools/rtsp_h264_check.py`）
  40 秒只拿到 **13 帧**；
* 把程序里的会话全部断开、同一个客户端单独拉，20 秒拿到 **300 帧**（15 fps），
  OpenCV 那条路更是 **29~30 fps**（911 帧 / 31.2 秒，最大间隔 428 ms，零卡顿）；
* 再验证"多客户端会不会互相拖累"：主流程 30 fps 不变，第二个客户端进来也不掉 ——
  所以不是"带宽不够"，而是**视频通道被某一个客户端占住时，别的客户端基本拿不到数据**。

结论（这份文件里的做法都是据此定的）：

1. 开流前只做 **TCP 探测**（``port_listening``），绝不自己再发 DESCRIBE 建会话；
2. 失败后**退避要耐心**（``RTSPS_BACKOFF_*``）：旧会话还没被打印机回收就急着重连，
   只会两条会话一起饿死；
3. 每帧都解码（这样"画面还在不在动"才统计得准），只在交付侧按帧率与"有没有人看"
   决定要不要编码 JPEG —— 用户把每路帧率调低时不该连画面一起变成幻灯片。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import socket
import threading
import time
from typing import Callable, Optional
from urllib.parse import quote

from .framegap import FrameGapTracker
from .ports import RTSP_PORT
from .timeouts import (
    RTSP_FIRST_FRAME_TIMEOUT,
    RTSP_OPEN_TIMEOUT_MS,
    RTSP_RETRY_PAUSE,
    RTSPS_BACKOFF_FACTOR,
    RTSPS_BACKOFF_MAX,
    RTSPS_DELIVERY_IDLE,
    RTSPS_HEALTHY_SECONDS,
    RTSPS_PORT_CHECK_TIMEOUT,
    RTSPS_STALE_READ_SECONDS,
)

LOGGER = logging.getLogger("bambu-monitor.rtsp")

#: 「连上了但一帧都没有」时给用户看到的说明。真机实测（2026-09-28）：
#: 一台打印机的实时画面同时只服务一个客户端 —— 我们的程序连着时另一个客户端
#: 只有 0.2 fps，程序断开后同一个客户端立刻 30 fps。所以这句话是有用的指引，
#: 而不是甩锅：用户关掉 Bambu Studio / 手机 App 里的同一路画面就恢复了。
STARVED_DETAIL = (
    "RTSPS 连上了但收不到画面：同一台打印机的实时画面同时只服务一个客户端，"
    "请先关掉 Bambu Studio / 手机 App / 其它网页里的同一路画面；"
    "若都没开，请确认打印机屏幕上「局域网实时画面」是开着的"
)

# 自签证书 + 强制 TCP 传输，必须在导入 cv2 之前设置
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|tls_verify;0")
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")


def port_listening(
    host: str, port: int = RTSP_PORT, timeout: float = RTSPS_PORT_CHECK_TIMEOUT
) -> bool:
    """``host:port`` 在不在监听（**只做 TCP 连接**，不建 RTSP 会话）。

    两个用途，都需要"不打扰画面通道"这件事：

    * `RtspStream` 开流前的快速筛子：322 没在监听（该机型没开「局域网实时画面」）
      就干脆跳过这一轮，不要让 FFmpeg 去白等；
    * `PrinterSession` 给 ``auto`` 机型选通道：322 在监听 → 走 RTSPS，
      否则 → 走 6000。这比"先试 RTSPS 超时了再退 6000"可靠得多，也不会来回跳。

    为什么不在这里发 DESCRIBE：那样会**建出一条 RTSP 会话**，而打印机同时只伺候
    一个画面客户端（见模块开头），探测把通道占住就等于自己把自己饿死。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001 - 探测失败就是"连不上"，绝不能把调用方带崩
        # 除了 OSError，离线测试环境还会让 socket 直接抛别的异常（conftest 的
        # `no_network` 守卫），所以这里必须兜住 Exception。
        return False


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
        #: 真正编码出来交付给界面的帧数（与 ``_frame_count`` 区分，见 :attr:`fps`）
        self._delivered = 0
        #: 最近一次有人来取帧、最近一次编码的时刻（决定还要不要继续编码）
        self._wanted_ts = 0.0
        self._last_encode_ts = 0.0
        #: 连续多少轮"连上了却一帧都没收到"（用来把原因说清楚，见 STARVED_DETAIL）
        self._starved_cycles = 0
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
        """取最新一帧 JPEG；**同时记下"有人在看"**（决定要不要继续编码，见 `_encode_due`）。"""
        with self._lock:
            self._wanted_ts = time.time()
            return self._latest_seq, self._latest

    def wait_first_frame(self, timeout: float = RTSP_FIRST_FRAME_TIMEOUT) -> Optional[bytes]:
        """等待首帧。

        ⚠️ ``retrying`` **不算失败**：打印机偶尔会拒掉一次新连接，流线程自己会退避重试，
        这时候继续等才可能等到首帧。只有 ``auth_error``（口令不对）与
        ``stopped``（已停）才值得提前放弃。
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
        """**交付给界面**的帧率（真正编码出来的帧）。

        与 :attr:`source_fps` 分开：解码照做（不然没法统计"流还活着"），
        编码只在有人看的时候按 ``max_fps`` 做 —— 所以这两个数可以差很多。
        """
        elapsed = time.time() - self._started_at if self._started_at else 0
        return self._delivered / elapsed if elapsed > 0 else 0.0

    @property
    def source_fps(self) -> float:
        """从打印机**收到**的帧率（解码速率）。"""
        elapsed = time.time() - self._started_at if self._started_at else 0
        return self._frame_count / elapsed if elapsed > 0 else 0.0

    @property
    def frame_count(self) -> int:
        """已解码的帧数（看门狗用它判断"画面还在不在动"）。"""
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
    def _encode_due(self, now: float) -> bool:
        """现在这一帧要不要编码成 JPEG 交付出去。

        * **第一帧一定编**（界面/网页都在等它）；
        * 最近 :data:`RTSPS_DELIVERY_IDLE` 秒没人来取帧就不编 —— 整面墙最小化、
          或者这一路没人看的时候，不该继续白烧 CPU（720p 解码约 0.3 个核，
          逐帧编码再多一点；手机/平板上更明显）；
        * 有人在看时按 ``max_fps`` 交付（用户设置的"每路帧率"，0 = 不限制）。
        """
        with self._lock:
            max_fps = self._max_fps
            last_encode = self._last_encode_ts
            last_wanted = self._wanted_ts
        if last_encode <= 0:  # 还没有交付过任何一帧
            return True
        if now - last_wanted > RTSPS_DELIVERY_IDLE:
            return False
        if max_fps <= 0:
            return True
        return (now - last_encode) >= (1.0 / max_fps)

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
        # ⚠️ 先进入「连接中」再干别的：`import cv2` 可能要好几百毫秒，而
        # `wait_first_frame()` 会把**初始的 stopped** 当成"已经停了"直接返回 None ——
        # 于是会话白白重启一轮（真机上表现为"刚开流就说没画面"）。
        self._set_state(self.STATE_CONNECTING, f"正在连接 RTSPS {self.host}:{RTSP_PORT}")
        try:
            import cv2  # noqa: PLC0415
        except ImportError:
            self._set_state(self.STATE_STOPPED, "未安装 opencv-python，无法使用 RTSPS")
            return

        backoff = RTSP_RETRY_PAUSE
        while not self._stop_event.is_set():
            path = self._paths[self._path_index % len(self._paths)]
            self._set_state(
                self.STATE_CONNECTING, f"正在连接 RTSPS {self.host}:{RTSP_PORT}{path}"
            )
            capture = None
            connected_at = 0.0
            try:
                # ⚠️ 开流前**只做一次 TCP 探测**（`port_listening`），不再自己发 DESCRIBE：
                # 真机实测（X1C，2026-09-28）一台打印机的实时画面同时只伺候一个客户端，
                # 多出来的连接不会让我们更快拿到画面，只会把仅有的那条通道搅乱。
                # TCP 探测不建会话，所以是安全的；确认没在监听就干脆跳过这一轮。
                if not port_listening(self.host, RTSP_PORT, timeout=RTSPS_PORT_CHECK_TIMEOUT):
                    raise RuntimeError(
                        "322 端口没有在监听：这台打印机没开「局域网实时画面 / LAN Mode Liveview」"
                    )
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
                        "无法打开 RTSPS 流（访问口令不对，或画面被别的客户端占着）"
                    )
                connected_at = time.time()
                cycle_frames = 0
                while not self._stop_event.is_set():
                    with self._lock:
                        target = self._target_size
                    # ⚠️ **不再按帧率节流再取帧**：那会让用户把每路帧率设成 4 时，
                    # 一路 30 fps 的画面真的只剩 4 fps（真机上就是"帧率奇低"）。
                    # 现在每一帧都 `grab()` —— 只把流往前推、**不做颜色转换**
                    # （实测 3.5 ms/帧 vs 5.8 ms/帧），于是"流还活着"、帧率、间隔都统计得准；
                    # 只有在**要交付**这一帧时才 `retrieve()` 出图像并按帧率编码。
                    read_started = time.time()
                    if not capture.grab():
                        # 「连上了却一帧都没收到」是最常见的一种失败，而且原因很具体：
                        # 这台打印机的实时画面被别的客户端占着（Bambu Studio / 手机 App /
                        # 另一个浏览器页面），或者那台机器根本没在出图。说清楚，别只说"重连"。
                        if cycle_frames == 0:
                            self._starved_cycles += 1
                        else:
                            self._starved_cycles = 0
                        if self._starved_cycles >= 1:
                            self._set_state(self.STATE_RETRYING, STARVED_DETAIL)
                        else:
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
                    now = time.time()
                    with self._lock:
                        self._last_frame_ts = now
                    self._gaps.note(now)
                    self._frame_count += 1
                    cycle_frames += 1
                    self._starved_cycles = 0
                    self.active_path = path
                    if self.state != self.STATE_STREAMING:
                        self._set_state(self.STATE_STREAMING, f"RTSPS 已连接（{path}）")
                    if not self._encode_due(now):
                        continue
                    # 要交付这一帧了：这时才做颜色转换（grab 与 retrieve 分开的理由见上）
                    ok, frame = capture.retrieve()
                    if not ok or frame is None:
                        continue
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
                        self._last_encode_ts = now
                    self._delivered += 1
                    if not self._first_frame_event.is_set():
                        self._first_frame_event.set()
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
            if self._stop_event.is_set():
                break
            # 退避：**连着取到帧够久才算"这台打印机是好的"**（退避清零、回头就快）；
            # 否则逐步加大间隔（上限 RTSPS_BACKOFF_MAX）—— 画面通道被占用时越急越拿不到。
            healthy = time.time() - connected_at if connected_at else 0.0
            if healthy >= RTSPS_HEALTHY_SECONDS:
                backoff = RTSP_RETRY_PAUSE
            else:
                backoff = min(backoff * RTSPS_BACKOFF_FACTOR, RTSPS_BACKOFF_MAX)
            if self._stop_event.wait(backoff):
                break
            backoff = min(backoff * 1.5, RTSPS_BACKOFF_MAX)
        self._set_state(self.STATE_STOPPED, "已停止")
