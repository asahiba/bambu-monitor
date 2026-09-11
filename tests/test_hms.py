"""HMS 错误码文案表的单元测试（``app/bambu/hms.py``）。

覆盖的契约：

* ``hms_text`` / ``display_code`` / ``wiki_url`` / ``error_text`` 的返回类型与格式
* ``wiki_url`` 一定是可点的 http(s) 地址（表里查不到时按通用规则拼官方 wiki 路径）
* 未知错误码（0 / 999999）不得抛异常
* ``data/*.json.gz`` 缺失时优雅降级：仍然返回错误码本身，而不是崩溃

本文件不联网：``wiki_url`` 只做字符串拼接，不发起任何请求。
"""

from __future__ import annotations

import gzip
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest

from app.bambu import hms


def _data_path(name: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(hms.__file__)), "data", name)


def _load_table(name: str) -> dict:
    """直接读压缩的文案表，用来和模块解析结果对照。"""
    with gzip.open(_data_path(name), "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _first_entry(section: str) -> tuple[str, str]:
    """从内置表里取一条 (16 位大写键, 文案)。"""
    table = _load_table("hms_zh_cn.json.gz")[section]
    assert isinstance(table, dict) and table, f"{section} 不该为空"
    raw_key, value = next(iter(table.items()))
    text = next(iter(value)) if isinstance(value, dict) else str(value)
    return str(raw_key).upper(), str(text)


# ------------------------------------------------------------------ 数据可用性
def test_内置数据文件存在且可解析():
    """契约：三份内置数据文件（中英 HMS 文案 + wiki 链接）都在且是合法 JSON。"""
    for name in ("hms_zh_cn.json.gz", "hms_en.json.gz", "wiki_links.json.gz"):
        assert os.path.exists(_data_path(name)), f"缺少数据文件 {name}"
        assert isinstance(_load_table(name), dict)


def test_data_available为真():
    """契约：内置文案表能加载时 data_available() 为 True。"""
    assert hms.data_available() is True


# ------------------------------------------------------------------ 组合键
@pytest.mark.parametrize(
    ("attr", "code", "expected"),
    [
        (0x03000100, 0x00010001, "0300010000010001"),
        (0, 0, "0000000000000000"),
        (-1, 0, "FFFFFFFF00000000"),
        (0xFFFFFFFF, 0xFFFFFFFF, "FFFFFFFFFFFFFFFF"),
    ],
)
def test_hms_key是8加8位大写十六进制(attr, code, expected):
    """契约：HMS 组合键 = 8 位 attr + 8 位 code 的大写十六进制（负数按无符号处理）。"""
    assert hms.hms_key(attr, code) == expected


def test_hms_key对非法输入返回空串():
    """契约：attr/code 不是数字时 hms_key 返回空串而不是抛异常。"""
    assert hms.hms_key(None, 1) == ""
    assert hms.hms_key(1, "abc") == ""


@pytest.mark.parametrize(
    ("attr", "code", "expected"),
    [
        (0x03000100, 0x00010001, "HMS_0300_0100_0001_0001"),
        (0, 0, "HMS_0000_0000_0000_0000"),
    ],
)
def test_display_code格式(attr, code, expected):
    """契约：display_code 返回可读短码 HMS_xxxx_xxxx_xxxx_xxxx。"""
    assert hms.display_code(attr, code) == expected


def test_display_code对非法输入返回空串():
    """契约：非法输入时 display_code 返回空串。"""
    assert hms.display_code(None, 0) == ""


# ------------------------------------------------------------------ 文案查询
def test_hms_text能查到内置中文文案():
    """契约：内置表里的 (attr, code) 必须能查出对应文案（键统一大写）。"""
    key, text = _first_entry("device_hms")
    attr, code = int(key[:8], 16), int(key[8:], 16)
    result = hms.hms_text(attr, code)
    assert isinstance(result, str)
    assert result == text
    assert result != ""


def test_error_text能查到内置故障文案():
    """契约：print_error 的 8 位码能查出中文说明。"""
    key, text = _first_entry("device_error")
    result = hms.error_text(int(key, 16))
    assert isinstance(result, str)
    assert result == text
    assert result != ""


def test_未知错误码不抛异常():
    """契约：查不到的码返回空字符串（或兜底文案），绝不抛异常。"""
    for code in (0, 999999, 0xFFFFFFFF, -1):
        assert isinstance(hms.error_text(code), str)
    for attr, code in ((0, 0), (0xDEADBEEF, 0xFEEDFACE), (-1, -1)):
        assert isinstance(hms.hms_text(attr, code), str)
        assert isinstance(hms.wiki_url(attr, code), str)
        assert isinstance(hms.display_code(attr, code), str)


def test_非法类型的查询不抛异常():
    """契约：收到 None / 字符串等非法入参时也应安全返回。"""
    assert hms.hms_text(None, None) == ""
    assert isinstance(hms.error_text(None), str)
    assert isinstance(hms.wiki_url(None, None), str)


# ------------------------------------------------------------------ wiki 链接
def test_wiki_url是可点击的http地址():
    """契约：wiki_url 必须以 http 开头，且指向 wiki.bambulab.com。"""
    url = hms.wiki_url(0x03000100, 0x00010001)
    assert url.startswith("http")
    assert "wiki.bambulab.com" in url


def test_wiki_url查不到时按通用规则拼路径():
    """契约：表里没有的码也要拼出官方 wiki 的 hmscode 路径，而不是返回空串。"""
    url = hms.wiki_url(0x12345678, 0x9ABCDEF0)
    assert url.startswith(hms.WIKI_BASE)
    assert "hmscode" in url
    assert "1234_5678_9ABC_DEF0" in url, "通用路径由大写组合键拼出"


def test_wiki_url命中内置链接表():
    """契约：wiki_links 表里的错误码应返回表里登记的完整地址。"""
    table = _load_table("wiki_links.json.gz")
    assert table, "wiki 链接表不该为空"
    raw_key, value = next(iter(table.items()))
    path = next(iter(value)) if isinstance(value, dict) else str(value)
    attr, code = int(str(raw_key)[:8], 16), int(str(raw_key)[8:], 16)
    url = hms.wiki_url(attr, code)
    assert url.startswith("http")
    assert url.endswith(path) or path in url


def test_display_code与wiki_url对同一条目保持一致():
    """契约：同一个 (attr, code) 的短码与 wiki 路径必须来自同一个组合键。"""
    key, _ = _first_entry("device_hms")
    attr, code = int(key[:8], 16), int(key[8:], 16)
    assert hms.display_code(attr, code) == (
        f"HMS_{key[0:4]}_{key[4:8]}_{key[8:12]}_{key[12:16]}"
    )
    assert key[0:4] in hms.wiki_url(attr, code)


# ------------------------------------------------------------------ 优雅降级
def test_数据文件缺失时优雅降级(monkeypatch, tmp_path):
    """契约（降级）：数据目录不存在时，模块必须仍能返回错误码而不是崩溃 ——
    ``hms_text``/``error_text`` 返回空串，``display_code``/``wiki_url`` 仍可用
    （用户至少还能看到原始错误码和官方解释页地址）。"""
    monkeypatch.setattr(hms, "DATA_DIR", str(tmp_path / "不存在的目录"))
    monkeypatch.setattr(hms, "_loaded", False)
    monkeypatch.setattr(hms, "_hms_text", {})
    monkeypatch.setattr(hms, "_error_text", {})
    monkeypatch.setattr(hms, "_wiki_links", {})

    assert hms.data_available() is False
    assert hms.hms_text(0x03000100, 0x00010001) == ""
    assert hms.error_text(0x0300400C) == ""
    assert hms.display_code(0x03000100, 0x00010001) == "HMS_0300_0100_0001_0001"
    url = hms.wiki_url(0x03000100, 0x00010001)
    assert url.startswith("http")
    assert "0300_0100_0001_0001" in url


def test_数据文件损坏时优雅降级(monkeypatch, tmp_path):
    """契约（降级）：文件存在但不是合法 gzip/JSON 时同样只退化、不抛异常。"""
    for name in ("hms_zh_cn.json.gz", "hms_en.json.gz", "wiki_links.json.gz"):
        (tmp_path / name).write_bytes(b"not a gzip stream")

    monkeypatch.setattr(hms, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(hms, "_loaded", False)
    monkeypatch.setattr(hms, "_hms_text", {})
    monkeypatch.setattr(hms, "_error_text", {})
    monkeypatch.setattr(hms, "_wiki_links", {})

    assert hms.data_available() is False
    assert hms.hms_text(1, 2) == ""
    assert hms.error_text(3) == ""
    assert hms.wiki_url(1, 2).startswith("http")


def test_降级之后仍可恢复加载(monkeypatch, tmp_path):
    """契约：把 DATA_DIR 指回真实目录后（_loaded 复位）文案表应重新加载成功，
    说明降级不会污染模块状态。"""
    monkeypatch.setattr(hms, "DATA_DIR", str(tmp_path / "不存在"))
    monkeypatch.setattr(hms, "_loaded", False)
    monkeypatch.setattr(hms, "_hms_text", {})
    monkeypatch.setattr(hms, "_error_text", {})
    monkeypatch.setattr(hms, "_wiki_links", {})
    assert hms.data_available() is False

    real_dir = os.path.dirname(_data_path("hms_zh_cn.json.gz"))
    monkeypatch.setattr(hms, "DATA_DIR", real_dir)
    monkeypatch.setattr(hms, "_loaded", False)
    assert hms.data_available() is True
    key, text = _first_entry("device_hms")
    attr, code = int(key[:8], 16), int(key[8:], 16)
    assert hms.hms_text(attr, code) == text
