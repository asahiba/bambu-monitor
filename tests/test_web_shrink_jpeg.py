"""网页端「大图缩放」的两条路径（``app/web/server.py``）。

## 为什么单独立一条

``_shrink_jpeg()`` 以前只走 Qt（``QImage``）。服务端部署
（``requirements-server.txt``，Docker / NAS / Linux）**故意不装 Qt**，于是
``import PySide6`` 抛异常 -> 返回 None -> 调用方「转码失败就用原图」->
**每一帧都是未缩放的原始大图**被推给手机，流量与内存成倍上涨，而且完全静默：
日志里一个字都没有，只有用户的流量账单能看出来。

现在补了 OpenCV 兜底（``requirements-server.txt`` 本来就装了
``opencv-python-headless``），本文件锁住三条路径：Qt / OpenCV / 都没有。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

# 这里用 OpenCV 造「大图」并验证缩放结果；没有 OpenCV 时整文件跳过，而不是报错。
# 以前是直接 `import numpy`：在既没 cv2 也没 numpy 的环境（比如精简的复现环境）里
# 会变成**收集期错误**，把整个测试会话打断，而不是干净地跳过这几条。
pytest.importorskip("cv2", reason="缩放路径的验证需要 OpenCV（CI 装了 opencv-python）")

import numpy as np  # noqa: E402

from app.web import server as web_server  # noqa: E402

pytestmark = pytest.mark.usefixtures("no_network")


def _big_jpeg(width: int = 1920, height: int = 1080, quality: int = 90) -> bytes:
    """造一张体积足够大（>PASSTHROUGH_BYTES）的随机噪声 JPEG。"""
    import cv2

    rng = np.random.default_rng(1234)
    # 纯噪声压缩不掉，用来稳定地造出「大图」
    image = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    assert ok
    return bytes(encoded.tobytes())


def _decoded_width(jpeg: bytes) -> int:
    import cv2

    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return int(image.shape[1])


def test_构造的测试图确实超过直通阈值():
    """前提校验：测试图必须大到会触发缩放，否则后面的断言没意义。"""
    assert len(_big_jpeg()) > web_server.PASSTHROUGH_BYTES


def test_opencv_路径能把大图缩到目标宽度(monkeypatch):
    """没有 Qt 时（服务端部署）必须靠 OpenCV 缩放，而不是退回原图。"""
    # 模拟「服务端没有 PySide6」
    monkeypatch.setattr(
        web_server, "_shrink_with_qt", lambda *a, **k: (_ for _ in ()).throw(ImportError("no Qt"))
    )
    source = _big_jpeg(1920)
    shrunk = web_server._shrink_jpeg(source, 720)
    assert shrunk is not None, "没有 Qt 就退回原图 = 手机流量翻倍（这正是原来的缺陷）"
    assert _decoded_width(shrunk) == 720
    assert len(shrunk) < len(source)


def test_opencv_路径保持宽高比(monkeypatch):
    monkeypatch.setattr(
        web_server, "_shrink_with_qt", lambda *a, **k: (_ for _ in ()).throw(ImportError("no Qt"))
    )
    shrunk = web_server._shrink_jpeg(_big_jpeg(1600, 900), 800)
    assert shrunk is not None
    import cv2

    image = cv2.imdecode(np.frombuffer(shrunk, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert (image.shape[1], image.shape[0]) == (800, 450)


def test_两条路径都不可用时返回_none_并只提示一次(monkeypatch, caplog):
    monkeypatch.setattr(
        web_server, "_shrink_with_qt", lambda *a, **k: (_ for _ in ()).throw(ImportError("no Qt"))
    )
    monkeypatch.setattr(
        web_server, "_shrink_with_cv2", lambda *a, **k: (_ for _ in ()).throw(ImportError("no cv2"))
    )
    monkeypatch.setattr(web_server, "_shrink_warned", False, raising=False)

    with caplog.at_level("WARNING", logger="bambu-monitor.web"):
        assert web_server._shrink_jpeg(_big_jpeg(320, 240), 720) is None
        assert web_server._shrink_jpeg(_big_jpeg(320, 240), 720) is None
    # 只提示一次：每帧都打会把日志刷爆
    assert len([r for r in caplog.records if "OpenCV" in r.message]) == 1


def test_坏数据不会抛异常(monkeypatch):
    """契约：不是 JPEG 的数据必须安全返回（调用方据此退回原图）。"""
    monkeypatch.setattr(
        web_server, "_shrink_with_qt", lambda *a, **k: (_ for _ in ()).throw(ImportError("no Qt"))
    )
    assert web_server._shrink_jpeg("这不是 JPEG".encode("utf-8"), 720) is None


def test_本来就不宽的画面不会被放大(monkeypatch):
    """契约：只缩不放。窄画面必须原样返回解码结果，不能插值放大。"""
    monkeypatch.setattr(
        web_server, "_shrink_with_qt", lambda *a, **k: (_ for _ in ()).throw(ImportError("no Qt"))
    )
    source = _big_jpeg(320, 240)
    shrunk = web_server._shrink_jpeg(source, 720)
    assert shrunk is not None
    assert _decoded_width(shrunk) == 320


def test_qt_路径同样能缩到目标宽度():
    """桌面版默认走 Qt：重构出 ``_shrink_with_qt`` 之后这条路径必须仍然可用。"""
    pyside = pytest.importorskip("PySide6.QtGui", reason="桌面环境才有 Qt")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    assert pyside is not None

    shrunk = web_server._shrink_with_qt(_big_jpeg(1920), 720)
    assert shrunk is not None
    assert _decoded_width(shrunk) == 720
