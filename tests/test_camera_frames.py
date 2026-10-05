"""6000 端口收帧循环的三个契约（都来自 2026-10-01 的真机实测）。

实测现场：

* **X1C / X2D** 在 6000 端口上回一个 **8 字节空包**（帧头 `flags=0`，
  负载 `FF FF FF FF 00 00 00 00`）然后断开 —— 表示"这个端口不提供画面"；
  而它们的访问代码是对的（MQTT 都连着）。
* **P1S / A1 / A2L** 在同一个端口上回 50~175 KB 的正常 JPEG（帧头 `flags=1`）。
* 口令**真的**错时，打印机是"发完鉴权包就把连接关掉"，连帧头都不给。

老代码只看长度（`size < 512` 就判"访问代码错误"），于是 X1C 界面上一直挂着
「访问代码错误」，用户去改一个本来就正确的口令，看门狗还在两条通道之间来回跳。
"""

from __future__ import annotations

import struct

import pytest

from app.bambu.camera import CameraStream

pytestmark = pytest.mark.usefixtures("no_network")


def _header(size: int, flags: int) -> bytes:
    return struct.pack("<IIII", size, 0x0003013F, flags, 2)


def test_空帧不算访问代码错误(monkeypatch):
    """契约：`flags=0` 的小包是"这个端口不提供画面"，按可重试处理。"""
    stream = CameraStream("10.0.0.1", "12345678")
    replies = [_header(8, 0), b"\xff\xff\xff\xff\x00\x00\x00\x00"]
    monkeypatch.setattr(
        stream, "_read_exact", lambda count, timeout=None: replies.pop(0) if replies else None
    )
    stream._pump()
    assert stream.state == CameraStream.STATE_RETRYING
    assert "空帧" in stream.detail, stream.detail


def test_发完鉴权包就被断开才算口令错(monkeypatch):
    """契约：对手连帧头都不给、直接关连接，才是"访问代码不对"。"""

    def fake(count, timeout=None):  # noqa: ANN001
        stream.peer_closed = True
        return None

    stream = CameraStream("10.0.0.1", "wrong-code")
    monkeypatch.setattr(stream, "_read_exact", fake)
    stream._pump()
    assert stream.state == CameraStream.STATE_AUTH_ERROR
    assert "访问代码" in stream.detail


def test_正常帧照旧入库(monkeypatch):
    """契约：`flags=1` + 足够大的 JPEG 仍然是正常帧（别把好数据也当拒绝）。"""
    seen: list[str] = []
    stream = CameraStream("10.0.0.1", "12345678", on_state=lambda state, detail: seen.append(state))
    jpeg = b"\xff\xd8" + b"\x00" * 600 + b"\xff\xd9"
    replies = [_header(len(jpeg), 1), jpeg, None]
    monkeypatch.setattr(
        stream, "_read_exact", lambda count, timeout=None: replies.pop(0) if replies else None
    )
    stream._pump()
    assert stream.frame_count == 1
    assert stream.latest_frame()[1] == jpeg
    assert CameraStream.STATE_STREAMING in seen, f"收到正常帧应当上报 streaming：{seen}"
