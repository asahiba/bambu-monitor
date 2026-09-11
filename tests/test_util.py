"""纯工具/纯函数层测试：``app/util/__init__.py`` 与 ``app/bambu/camera.py`` 的字节级辅助函数。

一、``configure_stdio()``
``configure_stdio`` 解决的问题：Windows 控制台默认 GBK(cp936)，自检脚本打印 ``✓``
或中文名会抛 ``UnicodeEncodeError``，表现为「自检成功却以退出码 1 结束」。
契约：把标准输出/错误切到 UTF-8 + errors="replace"，可重复调用，且对被替换/已关闭的流保持静默。

这里用受控的假流（包装 BytesIO 的 TextIOWrapper）验证，不依赖 pytest 自己的捕获对象。

二、``build_auth_packet()``
6000 端口视频流的 80 字节鉴权包（camera.py 模块 docstring 的表格）：
偏移 0/4 是小端 0x40 与 0x3000，8 起 8 字节为 0，16 起 32 字节用户名，48 起 32 字节访问代码，
两者都是 ASCII 右侧补 ``\\0``、超长截断。纯字节运算，不涉及任何 socket。
"""

from __future__ import annotations

import io
import struct
import sys

import pytest

from app.bambu import camera
from app.bambu.camera import build_auth_packet
from app.util import configure_stdio

CHECK_MARK = "✓"

#: 本文件是纯逻辑层测试：禁止任何 socket 连接/监听（不连真机、不占端口）
pytestmark = pytest.mark.usefixtures("no_network")


def _gbk_stream() -> tuple[io.TextIOWrapper, io.BytesIO]:
    """返回一个「GBK 严格模式」的文本流（模拟 Windows 中文控制台）与其底层缓冲。"""
    buffer = io.BytesIO()
    return io.TextIOWrapper(buffer, encoding="gbk", errors="strict"), buffer


def test_gbk_stream_cannot_encode_check_mark_without_configure():
    """前置事实：GBK 控制台直接写 ``✓`` 会抛 UnicodeEncodeError（这正是本函数存在的理由）。"""
    stream, _ = _gbk_stream()
    with pytest.raises(UnicodeEncodeError):
        stream.write(CHECK_MARK)
        stream.flush()


def test_configure_stdio_switches_streams_to_utf8(monkeypatch):
    """契约：调用后标准输出/标准错误都变成 UTF-8 + errors="replace"，``✓`` 与中文可正常写出。"""
    out, out_buffer = _gbk_stream()
    err, err_buffer = _gbk_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    configure_stdio()

    assert out.encoding.lower().replace("-", "") == "utf8"
    assert err.encoding.lower().replace("-", "") == "utf8"
    assert out.errors == "replace"
    assert err.errors == "replace"

    out.write(CHECK_MARK + " 打印机一")
    out.flush()
    assert out_buffer.getvalue() == (CHECK_MARK + " 打印机一").encode("utf-8")

    err.write(CHECK_MARK)
    err.flush()
    assert err_buffer.getvalue() == CHECK_MARK.encode("utf-8")


def test_configure_stdio_is_idempotent(monkeypatch):
    """契约：连续调用两次不抛异常，第二次不改变已经正确的设置（幂等，可在任意入口重复调用）。"""
    out, out_buffer = _gbk_stream()
    err, _ = _gbk_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    configure_stdio()
    configure_stdio()  # 第二次不得抛异常

    assert out.encoding.lower().replace("-", "") == "utf8"
    assert err.encoding.lower().replace("-", "") == "utf8"
    out.write(CHECK_MARK)
    out.flush()
    assert out_buffer.getvalue() == CHECK_MARK.encode("utf-8")


def test_configure_stdio_ignores_streams_without_reconfigure(monkeypatch):
    """契约（docstring）：流对象没有 ``reconfigure``（被重定向/替换）时静默跳过，不抛异常。"""
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    configure_stdio()  # 不应抛异常

    assert hasattr(sys.stdout, "reconfigure") is False


def test_configure_stdio_ignores_closed_stream(monkeypatch):
    """契约（docstring）：流已关闭时静默跳过（reconfigure 抛 ValueError 被吞掉）。"""
    closed_out, _ = _gbk_stream()
    closed_err, _ = _gbk_stream()
    closed_out.close()
    closed_err.close()
    monkeypatch.setattr(sys, "stdout", closed_out)
    monkeypatch.setattr(sys, "stderr", closed_err)

    configure_stdio()  # 不应抛异常

    assert closed_out.closed is True


# --------------------------------------------------------------------------- 80 字节鉴权包


def test_camera_protocol_constants():
    """契约（camera.py 模块 docstring）：鉴权包头与帧头的协议常量。"""
    assert camera.AUTH_INLINE_SIZE == 0x40
    assert camera.AUTH_TYPE == 0x3000
    assert camera.FRAME_HEADER_SIZE == 16
    assert camera.MIN_FRAME_SIZE < camera.MAX_FRAME_SIZE
    assert camera.JPEG_SOI == b"\xff\xd8"
    assert camera.AUTH_USERNAME == "bblp"


def test_build_auth_packet_length_and_header():
    """契约：``build_auth_packet`` 固定返回 80 字节，头部小端为 0x40 / 0x3000 / 0 / 0。"""
    packet = build_auth_packet("12345678")
    assert isinstance(packet, bytes)
    assert len(packet) == 80
    assert struct.unpack_from("<I", packet, 0)[0] == 0x40
    assert struct.unpack_from("<I", packet, 4)[0] == 0x3000
    assert struct.unpack_from("<I", packet, 8)[0] == 0
    assert struct.unpack_from("<I", packet, 12)[0] == 0
    assert packet[8:16] == b"\x00" * 8


def test_build_auth_packet_username_and_access_code_fields():
    """契约：偏移 16 起 32 字节是用户名 ``bblp`` 右补 ``\\0``；偏移 48 起 32 字节是访问代码右补 ``\\0``。"""
    packet = build_auth_packet("12345678")
    assert packet[16:48] == b"bblp" + b"\x00" * 28
    assert packet[48:80] == b"12345678" + b"\x00" * 24
    # 两个字段的分界必须精确落在第 48 字节
    assert packet[47] == 0
    assert packet[48:49] == b"1"


@pytest.mark.parametrize("length", [1, 8, 31, 32, 33, 64, 200])
def test_build_auth_packet_truncates_long_access_code(length):
    """契约：访问代码超过 32 字节时被截断（而不是让包长变化），不足部分右补 ``\\0``。"""
    code = "A" * length
    packet = build_auth_packet(code)

    assert len(packet) == 80
    filled = min(length, 32)
    assert packet[48 : 48 + filled] == b"A" * filled
    assert packet[48 + filled : 80] == b"\x00" * (32 - filled)


def test_build_auth_packet_truncates_long_username():
    """契约：用户名超过 32 字节同样截断，且不影响包长与访问代码的偏移。"""
    packet = build_auth_packet("12345678", username="u" * 40)

    assert len(packet) == 80
    assert packet[16:48] == b"u" * 32
    assert packet[48:80] == b"12345678" + b"\x00" * 24


@pytest.mark.parametrize(
    "code",
    ["密码123", "密码", "🔒🔒", "тest-code", "abc密码def"],
    ids=["mixed", "all-non-ascii", "emoji", "cyrillic", "interleaved"],
)
def test_build_auth_packet_ignores_non_ascii(code):
    """契约：非 ASCII 字符按 ``errors="ignore"`` 丢弃而非抛异常，包长仍是 80。"""
    packet = build_auth_packet(code)
    expected = code.encode("ascii", errors="ignore")

    assert len(packet) == 80
    assert packet[48 : 48 + len(expected)] == expected
    assert packet[48 + len(expected) : 80] == b"\x00" * (32 - len(expected))


def test_build_auth_packet_with_empty_access_code():
    """契约：空访问代码生成全 0 的访问代码字段（仍可通过长度断言，不抛异常）。"""
    packet = build_auth_packet("")
    assert len(packet) == 80
    assert packet[48:80] == b"\x00" * 32
    assert packet[16:48] == b"bblp" + b"\x00" * 28

