"""``app/util/secret.py`` 凭据处理契约测试。

覆盖：``token_hex`` 的长度下限与随机性、DPAPI 加解密往返、空串与明文兼容路径、
``is_encrypted`` 判定，以及 DPAPI 不可用时的降级行为（加密退回明文 / 解密退回空串）。

本文件不需要联网，也不读写任何真实配置文件。
"""

from __future__ import annotations

import string
import sys

import pytest

from app.util import secret

_HEX_DIGITS = set(string.hexdigits.lower())

#: 本文件是纯逻辑层测试：禁止任何 socket 连接/监听（不连真机、不占端口）
pytestmark = pytest.mark.usefixtures("no_network")

#: DPAPI 只在 Windows 上存在；其余断言在任意平台都成立
requires_dpapi = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 仅在 Windows 上可用")


# --------------------------------------------------------------------------- token_hex


def test_token_hex_returns_hex_string_of_expected_length():
    """契约：``token_hex(8)`` 返回 16 个十六进制字符（secrets.token_hex(8) 的双倍长度）。"""
    token = secret.token_hex(8)
    assert isinstance(token, str)
    assert len(token) == 16
    assert set(token.lower()) <= _HEX_DIGITS


def test_token_hex_is_random_across_calls():
    """契约：两次调用不相等（每次都是新的随机令牌，不能是常量）。"""
    tokens = {secret.token_hex(8) for _ in range(5)}
    assert len(tokens) == 5


@pytest.mark.parametrize(
    "length, expected_len",
    [(0, 8), (1, 8), (2, 8), (3, 8), (4, 8), (16, 32)],
)
def test_token_hex_has_minimum_length(length, expected_len):
    """契约：``max(4, length)`` 下限——请求长度 <4 时按 4 字节生成，即至少 8 个字符。"""
    token = secret.token_hex(length)
    assert len(token) == expected_len
    assert set(token.lower()) <= _HEX_DIGITS


# --------------------------------------------------------------------------- DPAPI 往返


@requires_dpapi
def test_encrypt_decrypt_roundtrip_for_access_code():
    """契约：Windows 上 ``encrypt_text`` 输出以 "dpapi:" 开头，``decrypt_text`` 能还原原文。"""
    plain = "12345678"
    blob = secret.encrypt_text(plain)
    assert blob.startswith("dpapi:")
    assert blob != plain  # 必须是密文，不能原样返回
    assert secret.decrypt_text(blob) == plain


@requires_dpapi
@pytest.mark.parametrize(
    "plain",
    ["a", "12345678", "访问代码 abcXYZ-123", "带 emoji 的口令🔒", "x" * 512],
    ids=["single-char", "eight-digits", "chinese", "emoji", "long"],
)
def test_encrypt_decrypt_roundtrip_for_various_texts(plain):
    """契约：ASCII / 中文 / emoji / 超长文本往返一致（内部按 UTF-8 编码）。"""
    blob = secret.encrypt_text(plain)
    assert blob.startswith("dpapi:")
    assert secret.decrypt_text(blob) == plain


@requires_dpapi
def test_encrypt_text_is_not_deterministic_but_still_decryptable():
    """契约：DPAPI 每次加密结果不同（带随机盐），但都能解回同一原文。"""
    first = secret.encrypt_text("12345678")
    second = secret.encrypt_text("12345678")
    assert first != second
    assert secret.decrypt_text(first) == secret.decrypt_text(second) == "12345678"


# --------------------------------------------------------------------------- 空串与明文兼容


def test_empty_text_is_passthrough_both_ways():
    """契约：``encrypt_text("")`` 与 ``decrypt_text("")`` 都返回空串（不做无意义的加解密）。"""
    assert secret.encrypt_text("") == ""
    assert secret.decrypt_text("") == ""


@pytest.mark.parametrize("plain", ["明文没有前缀", "12345678", "dpapi", "DPAPI:xxx"])
def test_decrypt_plaintext_without_prefix_returns_as_is(plain):
    """契约（向后兼容）：没有 "dpapi:" 前缀的字符串视为未加密，原样返回。"""
    assert secret.decrypt_text(plain) == plain


@pytest.mark.parametrize(
    "text, expected",
    [
        ("dpapi:xxx", True),
        ("dpapi:", True),
        ("", False),
        ("abc", False),
        ("明文没有前缀", False),
        ("DPAPI:xxx", False),  # 前缀大小写敏感
        (" dpapi:xxx", False),
    ],
)
def test_is_encrypted(text, expected):
    """契约：只有以 "dpapi:" 开头的非空字符串才算已加密（大小写敏感）。"""
    assert secret.is_encrypted(text) is expected


# --------------------------------------------------------------------------- 降级路径


@requires_dpapi
@pytest.mark.parametrize(
    "broken",
    ["dpapi:not-base64!!", "dpapi:AAAA", "dpapi:"],
    ids=["not-base64", "garbage-blob", "empty-payload"],
)
def test_decrypt_broken_dpapi_payload_returns_empty(broken):
    """契约：以 "dpapi:" 开头却解不开的串（换机器/换 Windows 用户）返回空串，不抛异常。"""
    assert secret.decrypt_text(broken) == ""


def test_encrypt_text_falls_back_to_plaintext_when_dpapi_unavailable(monkeypatch):
    """契约（模块 docstring）：DPAPI 调用失败时 ``encrypt_text`` 原样返回明文，保证功能可用。"""

    def _boom(data, protect):
        raise OSError("模拟没有 DPAPI")

    monkeypatch.setattr(secret, "_crypt", _boom)
    assert secret.encrypt_text("12345678") == "12345678"


def test_decrypt_text_returns_empty_when_dpapi_unavailable(monkeypatch):
    """契约（模块 docstring）：DPAPI 调用失败时 ``decrypt_text`` 返回空串而不是抛异常。"""

    def _boom(data, protect):
        raise OSError("模拟没有 DPAPI")

    monkeypatch.setattr(secret, "_crypt", _boom)
    assert secret.decrypt_text("dpapi:AAAA") == ""
