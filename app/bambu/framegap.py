"""记录「这台设备自己的出帧节奏」，给「画面还在不在」的判据用。

## 为什么需要它

判「画面断没断」以前是写死一个秒数（6 秒），这对**慢通道**根本不成立：

真机实测（2026-09，用户家里 17 台设备，从正在运行的实例里量出来的）：

======================  ==========  ==========  ==========
设备                    间隔中位     间隔最大     等效帧率
======================  ==========  ==========  ==========
P1S 系列（6000 端口）    2.8~7.6 秒   12~20 秒    0.16~0.28
A1 / A1 mini            6.4~8.8 秒   20~21 秒    0.12
A2L / 未连上              —           —          0
RTSPS（X2D）             0.1 秒       —           约 9
======================  ==========  ==========  ==========

也就是说 6000 端口的机器**十几秒没有新帧是常态**，拿 6 秒去判，`camera_online`
每几秒就翻一次（真机上实测 90 秒里翻了 2~8 次），界面上就是「在线/离线」乱闪。

而且**不能用平均帧率去估**：打印机在刚连上时会先吐几张（把平均值抬高到
0.5 fps），随后才进入几秒一张的稳态 —— 用平均值算出来的门槛仍然太短。

所以这里记的是**最近几次真实间隔里的最大值**：这台设备平时几秒一张、
偶尔十几秒一张，都算它自己的正常节奏；超过这个节奏 1.5 倍还没帧，才算断。
"""

from __future__ import annotations

import threading
import time
from collections import deque

#: 保留最近多少个间隔。太少会被一次抖动长期抬高、又很快滑出去（门槛忽高忽低），
#: 太多则反应迟钝。8 个间隔在 6000 端口的机器上约等于最近 40 秒。
GAP_WINDOW = 8


class FrameGapTracker:
    """线程安全的帧间隔记录器（写入在收帧线程，读取在界面/网页线程）。"""

    def __init__(self, window: int = GAP_WINDOW) -> None:
        self._window = max(2, int(window))
        self._gaps: deque[float] = deque(maxlen=self._window)
        self._last_ts = 0.0
        self._lock = threading.Lock()

    def note(self, timestamp: float | None = None) -> None:
        """记下一帧到达的时刻（收帧线程调用，必须很快返回）。"""
        now = time.time() if timestamp is None else float(timestamp)
        with self._lock:
            if self._last_ts > 0:
                gap = now - self._last_ts
                if gap > 0:
                    self._gaps.append(gap)
            self._last_ts = now

    @property
    def max_gap(self) -> float:
        """最近若干个间隔里的最大值（秒）；还没有两次帧时返回 0。

        ⚠️ 断线重连时**不清空**：这台设备的节奏不会因为重连而改变，
        清空反而会让刚接上的那几秒退回最短门槛、又开始闪。
        """
        with self._lock:
            return max(self._gaps) if self._gaps else 0.0
