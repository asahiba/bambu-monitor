"""Moonraker 摄像头：URL 解析与多路画面（对着 Voron 2.4 真机踩出来的两个坑）。

## 坑一：相对 URL 不能拼在 Moonraker 端口上（这就是"有摄像头却永远没画面"）

真机实测（Voron 2.4 + crowsnest，2026-09）：

```
GET /server/webcams/list        → snapshot_url: "/webcam/?action=snapshot"
GET http://<ip>/webcam/?action=snapshot       → 200 image/jpeg  28 KB   ← 能出画面
GET http://<ip>:7125/webcam/?action=snapshot  → 404 application/json      ← 程序原来拼的是这个
```

``/webcam/`` 是 nginx 代理到 crowsnest 的 ustreamer，挂在**主机 80 端口**上；
Moonraker 自己的端口后面没有这个路径。所以相对地址要按「主机根」解析，
Moonraker 端口只作为兜底（有些部署确实挂在它后面），谁成功就记住谁。

## 坑二：一台机器可能有多路画面

Voron 常见配置是「喷嘴 + 舱内」两个摄像头（``/webcam/`` ``/webcam2/`` …）。
程序原来只认第一路，剩下几路等于不存在。现在 ``cameras()`` 列出全部，
``latest_frame(index)`` 可以取任意一路，``enabled: false`` 的不登记
（它只会每次都失败，把画面状态带成"不可用"）。
"""

from __future__ import annotations

import time

import pytest

from app.adapters.moonraker.adapter import MoonrakerAdapter
from app.adapters.moonraker.fake import TINY_JPEG, FakeMoonraker
from app.bambu.models import PrinterInfo


def make_info() -> PrinterInfo:
    return PrinterInfo(ip="127.0.0.1", name="Voron 假机")


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _adapter(fake: FakeMoonraker, **kwargs) -> MoonrakerAdapter:
    host, _, port = fake.base_url.rpartition(":")
    return MoonrakerAdapter(
        PrinterInfo(ip=host.replace("http://", ""), port=int(port), name="Voron 假机"),
        base_url=fake.base_url,
        poll_interval=0.1,
        **kwargs,
    )


# ------------------------------------------------------------------ URL 解析
def test_相对快照地址按主机根解析而不是Moonraker端口():
    """契约：``/webcam/…`` 这类相对地址要拼在主机根上（真机实测端口 80 才对）。

    拼在 Moonraker 端口上会 404 —— 这正是"连通了但永远没有画面"的原因。
    """
    adapter = MoonrakerAdapter(make_info(), base_url="http://192.168.31.55:7125")
    try:
        assert adapter._resolve("/webcam/?action=snapshot", "http://192.168.31.55") == (
            "http://192.168.31.55/webcam/?action=snapshot"
        )
        assert adapter._resolve("webcam/x.jpg", "http://192.168.31.55:7125") == (
            "http://192.168.31.55:7125/webcam/x.jpg"
        )
        # 已经是绝对地址就原样用（用户手填的场景）
        assert adapter._resolve("http://cam.local/s.jpg", "http://x") == "http://cam.local/s.jpg"
        assert adapter._candidate_bases()[0] == "http://192.168.31.55"
        assert "http://192.168.31.55:7125" in adapter._candidate_bases()
    finally:
        adapter.stop()


def test_主机根拿不到时退回Moonraker端口():
    """契约：两条基准都试，谁成功用谁（不写死"一定在 80 端口"）。

    假服务器只在自己的端口上提供快照，主机根（127.0.0.1:80）连不上，
    所以这里走的就是"兜底也有效"这条路径。
    """
    with FakeMoonraker() as fake:
        adapter = _adapter(fake)
        try:
            adapter.start()
            assert _wait_until(lambda: adapter.latest_frame()[1] is not None), "没取到画面"
            assert adapter._camera_base == fake.base_url, (
                f"应该记住能用的那个基准，实际记住了 {adapter._camera_base!r}"
            )
        finally:
            adapter.stop()


# ------------------------------------------------------------------ 多路画面
def test_多路摄像头都能列出来():
    """契约：``cameras()`` 列出全部可用摄像头，并给出解析后的地址。"""
    with FakeMoonraker(camera="multi") as fake:
        adapter = _adapter(fake)
        try:
            adapter.start()
            cameras = adapter.cameras()
            assert [item["name"] for item in cameras] == ["camera", "nozzle"]
            assert [item["index"] for item in cameras] == [0, 1]
            assert cameras[1]["location"] == "nozzle"
            # 第二路的快照地址同样按基准解析过，便于用户核对
            assert cameras[1]["url"].endswith("/camera2/monitor.jpg")
        finally:
            adapter.stop()


def test_第二路画面能单独取到():
    """契约：``latest_frame(1)`` 取的是第二路，和主画面互不影响。

    两路假画面的长度不同，所以能断言"取到的确实是另一路"。
    """
    with FakeMoonraker(camera="multi") as fake:
        adapter = _adapter(fake)
        try:
            adapter.start()
            assert _wait_until(lambda: adapter.latest_frame(1)[1] is not None), "第二路没取到画面"
            _seq0, first = adapter.latest_frame(0)
            _seq1, second = adapter.latest_frame(1)
            assert first and second
            assert first != second, "两路画面内容一样，说明取的是同一路"
            assert first.startswith(b"\xff\xd8") and second.startswith(b"\xff\xd8")
            assert len(second) == len(TINY_JPEG) + 16
        finally:
            adapter.stop()


def test_设备端禁用的摄像头不登记():
    """契约：``enabled: false`` 的摄像头直接跳过 —— 它只会每次都失败。"""
    with FakeMoonraker(camera="multi_disabled") as fake:
        adapter = _adapter(fake)
        try:
            adapter.start()
            names = [item["name"] for item in adapter.cameras()]
            assert names == ["camera"], f"禁用的摄像头不该出现在列表里：{names}"
        finally:
            adapter.stop()


def test_没插上的摄像头列为不可用而不是拖垮主画面():
    """契约：一路取不到帧不影响另一路，失败原因按路记录。"""
    with FakeMoonraker(camera="multi") as fake:
        # 让第二路指向一个不存在的路径，模拟"配了但没插上"
        adapter = _adapter(fake)
        try:
            adapter.start()
            adapter._cameras[1]["path"] = "/camera3/monitor.jpg"
            adapter._cameras[1]["base"] = fake.base_url
            assert _wait_until(lambda: adapter._cameras[1]["fails"] > 0), "第二路没被标记失败"
            assert adapter.latest_frame(0)[1] is not None, "主画面不该受影响"
            cameras = adapter.cameras()
            assert cameras[0]["available"] is True
            assert cameras[1]["available"] is False
            assert "404" in cameras[1]["detail"] or "不是 JPEG" in cameras[1]["detail"]
        finally:
            adapter.stop()


# ------------------------------------------------------------------ 保活
def test_设备不认识保活方法时不再反复调用():
    """契约：``camera.start_monitor`` 返回 -32601 就永久关掉保活。

    普通 Moonraker（Voron 实测）没有这个方法，而 U1 必须有。以前的做法是
    每 5 秒试一次、每次打一条告警 —— 真出问题时那些噪音会把线索淹掉。
    """
    with FakeMoonraker(camera_monitor=False) as fake:
        # FakeMoonraker 启动时会自带一个 WS 服务（与真机一样是同端口升级）
        adapter = _adapter(fake, ws_url=fake._ws.url)
        try:
            adapter._camera_url = "/camera/monitor.jpg"
            adapter._last_keepalive = 0.0
            adapter._keepalive_supported = None
            adapter._maybe_keepalive()
            assert adapter._keepalive_supported is False, "应该记住这台设备不支持保活"
            # 再调一次：因为已经关掉，不该再发出任何 WS 调用（连接数不会涨）
            calls_before = fake.ws_calls[:]
            adapter._last_keepalive = 0.0
            adapter._maybe_keepalive()
            assert fake.ws_calls == calls_before, "关掉之后不该再发调用"
        finally:
            adapter.stop()


@pytest.mark.parametrize("mode", ["snapshot", "list"])
def test_只有一路时行为与以前一致(mode):
    """契约：单摄像头的部署（绝大多数）路径不变。"""
    with FakeMoonraker(camera=mode) as fake:
        adapter = _adapter(fake)
        try:
            adapter.start()
            assert _wait_until(lambda: adapter.latest_frame()[1] is not None)
            assert adapter.capabilities.has_camera is True
            assert adapter.video_backend == "快照"
            assert len(adapter.cameras()) == 1
        finally:
            adapter.stop()
