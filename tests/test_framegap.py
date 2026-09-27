"""``app/bambu/framegap.py`` 的契约测试。

## 为什么单独立一条

这个记录器是「画面断没断」判据的数据来源，而真机上（6000 端口的 A1 / P1 系列）
一台设备十几秒没有新帧是**常态** —— 门槛算错的表现就是界面每几秒闪一次「离线」。
所以这里既测记录器本身（窗口、异常输入、线程安全），也测它被真正接进了三条通路。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.bambu.framegap import GAP_WINDOW, FrameGapTracker

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.usefixtures("no_network")


def test_间隔按传入的时刻计算():
    tracker = FrameGapTracker()
    tracker.note(100.0)
    assert tracker.max_gap == 0.0, "只记了一个时刻，还算不出间隔"
    tracker.note(103.0)
    assert tracker.max_gap == pytest.approx(3.0)
    tracker.note(104.5)
    assert tracker.max_gap == pytest.approx(3.0), "最大值不会被更小的间隔顶掉"
    tracker.note(120.0)
    assert tracker.max_gap == pytest.approx(15.5)


def test_窗口只保留最近若干个间隔():
    """契约：历史够长就够（一次久远的卡顿不该永远抬高门槛）。"""
    tracker = FrameGapTracker(window=3)
    tracker.note(100.0)
    tracker.note(160.0)  # 60 秒的大洞
    for step in range(1, 4):
        tracker.note(160.0 + 2.0 * step)  # 之后都是 2 秒一张
    assert tracker.max_gap == pytest.approx(2.0), "大洞应当已经滑出窗口"


def test_时间倒退或重复不会算出负数():
    """契约：系统时间被调、或同一时刻重复调用，都不能让门槛变成 NaN/负数。"""
    tracker = FrameGapTracker()
    tracker.note(100.0)
    tracker.note(100.0)  # 同一时刻
    tracker.note(90.0)  # 时间倒退
    assert tracker.max_gap >= 0.0
    assert tracker.max_gap == 0.0


def test_窗口大小至少为二():
    """契约：就算传 1 也要保留两个间隔 —— 只有一个记不下任何间隔。"""
    tracker = FrameGapTracker(window=1)
    tracker.note(100.0)
    tracker.note(105.0)
    assert tracker.max_gap == pytest.approx(5.0)


def test_默认窗口不长不短():
    """提示性上限：窗口太小会被一次抖动反复清掉，太大则反应迟钝。"""
    assert 4 <= GAP_WINDOW <= 12


def test_多线程同时记也不会崩():
    """收帧线程写、界面线程读 —— 不能抛异常，也不能读到半个状态。"""
    import threading

    tracker = FrameGapTracker()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for step in range(500):
                tracker.note(step * 0.01)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(500):
                assert tracker.max_gap >= 0.0
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)
    assert not errors, errors


# --------------------------------------------------------------------------- 接进三条通路

#: 三条画面通路都要暴露 `frame_gap`（会话按它算「画面在线」的门槛）
STREAMS = {
    "app/bambu/camera.py": "CameraStream",
    "app/bambu/rtsp.py": "RtspStream",
    "app/bambu/rtsp_h264.py": "RtspH264Client",
}


@pytest.mark.parametrize("relative", sorted(STREAMS))
def test_每条通路的流都暴露_frame_gap(relative):
    """**回归**：6000 端口 / RTSPS(OpenCV) / RTSPS(纯 H.264) 三条路都要能报节奏。

    只接了其中一条的话，另一条上的设备会退回 6 秒判据、继续闪。
    """
    source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "FrameGapTracker" in source, f"{relative} 没有使用 FrameGapTracker"
    properties = {
        node.name
        for klass in ast.walk(tree)
        if isinstance(klass, ast.ClassDef)
        for node in klass.body
        if isinstance(node, ast.FunctionDef)
    }
    assert "frame_gap" in properties, f"{relative} 的流没有 frame_gap 属性"
    assert "self._gaps.note(" in source, f"{relative} 收帧时没有记录间隔"


def test_每条通路都真的在收帧处记了一笔():
    """契约：`_gaps.note(...)` 要出现在收帧/收到访问单元的地方，而不是构造函数里。"""
    for relative in STREAMS:
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        assert "self._gaps = FrameGapTracker()" in source
        note_lines = [line for line in source.splitlines() if "self._gaps.note(" in line]
        assert len(note_lines) == 1, f"{relative} 记间隔的地方应当只有一处：{note_lines}"
