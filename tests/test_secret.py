"""``app/util/secret.py`` 凭据处理契约测试。

覆盖：``token_hex`` 的长度下限与随机性、DPAPI 加解密往返、空串与明文兼容路径、
``is_encrypted`` 判定，以及 DPAPI 不可用时的降级行为（加密退回明文 / 解密退回空串）。

本文件不需要联网，也不读写任何真实配置文件。
"""

from __future__ import annotations

import os
import string
import subprocess
import sys
from pathlib import Path

import pytest

from app.util import secret

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

try:  # 安卓 APK 刻意不含 cryptography，所以它是可选依赖
    import cryptography  # noqa: F401

    HAS_CRYPTOGRAPHY = True
except ImportError:  # pragma: no cover - 只有精简部署才会走到
    HAS_CRYPTOGRAPHY = False

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
    """契约（模块 docstring）：DPAPI **且** 加密库都不可用时原样返回明文，保证功能可用。

    这是**安卓**的处境：APK 刻意不装 ``cryptography``
    （Chaquopy 的预编译包是 4096 字节对齐，会在 16 KB 内存页设备上闪退，
    见 ``docs/PACKAGING.md`` 与 ``tests/test_android_packaging.py``）。
    """

    def _boom(data, protect):
        raise OSError("模拟没有 DPAPI")

    monkeypatch.setattr(secret, "_crypt", _boom)
    monkeypatch.setattr(secret, "_fernet", lambda: None)  # 模拟没有 cryptography
    assert secret.encrypt_text("12345678") == "12345678"
    assert secret.last_error() is None, "平台不给加密能力是事实，不是这次操作失败"


def test_decrypt_text_returns_empty_when_dpapi_unavailable(monkeypatch):
    """契约（模块 docstring）：DPAPI 调用失败时 ``decrypt_text`` 返回空串而不是抛异常。"""

    def _boom(data, protect):
        raise OSError("模拟没有 DPAPI")

    monkeypatch.setattr(secret, "_crypt", _boom)
    assert secret.decrypt_text("dpapi:AAAA") == ""


# --------------------------------------------------------------------------- 本机密钥（非 Windows）


def _no_dpapi(monkeypatch):
    """模拟「没有 DPAPI」（Linux / Docker / 安卓 都走这条路）。"""
    monkeypatch.setattr(secret, "_crypt", lambda data, protect: (_ for _ in ()).throw(OSError("DPAPI 仅在 Windows 上可用")))
    monkeypatch.setattr(secret.sys, "platform", "linux")


requires_cryptography = pytest.mark.skipif(
    not HAS_CRYPTOGRAPHY, reason="本机密钥加密需要 cryptography"
)


@requires_cryptography
def test_没有DPAPI时用本机密钥加密而不是明文落盘(monkeypatch, isolated_config_dir):
    """**核心回归**：Linux / Docker 不再把访问代码明文写进 config.json。

    以前这里退回明文，只在配置目录权限上做文章；现在有 ``cryptography`` 时用
    32 字节随机密钥（配置目录下的 ``secret.key``，0600）+ Fernet 加密。
    """
    _no_dpapi(monkeypatch)
    blob = secret.encrypt_text("12345678")
    assert blob.startswith("fernet:"), blob
    assert "12345678" not in blob
    assert secret.decrypt_text(blob) == "12345678"
    assert secret.last_error() is None, "平台没有 DPAPI 是事实，不是这次操作失败"
    assert secret.last_warning(), "应告诉用户凭据是怎么保护的"

    key_file = isolated_config_dir / "secret.key"
    assert key_file.exists(), "非 Windows 上应当生成密钥文件"
    # 密钥文件必须只有本人可读（0600）；先写内容后 chmod 会留下"世界可读"的窗口。
    # 这里用 os.name 而不是 sys.platform：本测试把 sys.platform 改成了 linux 以
    # 模拟非 Windows，而 Windows 上 chmod 是空操作（模式位恒为 0666）。
    if os.name == "posix":
        assert (key_file.stat().st_mode & 0o077) == 0, oct(key_file.stat().st_mode)


@requires_cryptography
def test_另一个进程用同一个密钥文件也能解开(monkeypatch, isolated_config_dir, tmp_path):
    """契约：密钥文件是**跨进程持久**的 —— 重启后必须还能解开老配置。

    用子进程而不是 ``monkeypatch.undo()``：后者会把 autouse 的配置目录隔离一起
    撤销（同一个 monkeypatch 实例），于是测试会去碰用户真实的
    ``%APPDATA%\\BambuMonitor``。
    """
    _no_dpapi(monkeypatch)
    blob = secret.encrypt_text("87654321")

    script = tmp_path / "child_decrypt.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(_PROJECT_ROOT)!r})\n"
        "from app.util import secret\n"
        "print(secret.decrypt_text(sys.argv[1]))\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["BAMBU_MONITOR_CONFIG_DIR"] = str(isolated_config_dir)
    env.pop("BAMBU_MONITOR_SECRET", None)
    env.pop("BAMBU_MONITOR_KEY_FILE", None)

    result = subprocess.run(
        [sys.executable, str(script), blob],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_PROJECT_ROOT),
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "87654321"


@requires_cryptography
def test_密钥文件丢失时给出明确提示而不是抛异常(monkeypatch, isolated_config_dir):
    _no_dpapi(monkeypatch)
    blob = secret.encrypt_text("12345678")
    (isolated_config_dir / "secret.key").unlink()
    # 重新生成一把新密钥（文件丢失后的真实情形）
    assert secret.decrypt_text(blob) == ""
    assert "密钥文件" in (secret.last_warning() or "")


@requires_cryptography
def test_环境变量口令优先于密钥文件(monkeypatch, isolated_config_dir):
    """契约：设置了 ``BAMBU_MONITOR_SECRET`` 就不落盘密钥文件（最强的一档）。"""
    _no_dpapi(monkeypatch)
    monkeypatch.setenv("BAMBU_MONITOR_SECRET", "我的口令")
    blob = secret.encrypt_text("12345678")
    assert blob.startswith("fernet:")
    assert not (isolated_config_dir / "secret.key").exists(), "用口令时不该生成密钥文件"
    assert secret.decrypt_text(blob) == "12345678"
    assert "BAMBU_MONITOR_SECRET" in (secret.last_warning() or "")

    # 换一个口令 -> 解不开（而且不抛异常）
    monkeypatch.setenv("BAMBU_MONITOR_SECRET", "别的口令")
    assert secret.decrypt_text(blob) == ""


@requires_cryptography
def test_可以指定密钥文件路径(monkeypatch, tmp_path):
    """契约：``BAMBU_MONITOR_KEY_FILE`` 让 Docker 用户把密钥挂到别处（secrets）。"""
    _no_dpapi(monkeypatch)
    target = tmp_path / "secrets" / "bm.key"
    target.parent.mkdir()
    monkeypatch.setenv("BAMBU_MONITOR_KEY_FILE", str(target))
    blob = secret.encrypt_text("12345678")
    assert target.exists()
    assert secret.decrypt_text(blob) == "12345678"


@requires_cryptography
def test_can_encrypt_反映真实能力(monkeypatch):
    assert secret.can_encrypt() is True
    monkeypatch.setattr(secret, "_fernet", lambda: None)
    monkeypatch.setattr(secret.sys, "platform", "linux")
    assert secret.can_encrypt() is False, "没有加密库时必须如实报告「只能明文」"


def test_is_encrypted_认识两种前缀():
    """契约：``fernet:`` 与 ``dpapi:`` 都算已加密；明文（安卓降级）不算。"""
    assert secret.is_encrypted("fernet:abc") is True
    assert secret.is_encrypted("12345678") is False
