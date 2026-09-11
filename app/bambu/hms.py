"""HMS 错误码 / 故障码文案解析。

数据来自 ha-bambulab 项目的 HMS 文案表（MIT 许可），压缩后随程序分发：

* ``device_hms``：``hms`` 数组里的 (attr, code) 组合，键为 16 位十六进制
* ``device_error``：``print_error`` 的 8 位十六进制码
* ``wiki_links``：错误码对应的官方 wiki 页面路径

数据文件缺失时（例如打包时漏了 data 目录）会优雅退化为「只显示原始错误码」。
"""

from __future__ import annotations

import gzip
import json
import os
import threading
from typing import Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
WIKI_BASE = "https://wiki.bambulab.com"

_lock = threading.Lock()
_loaded = False
_hms_text: dict[str, str] = {}
_error_text: dict[str, str] = {}
_wiki_links: dict[str, str] = {}


def _load_file(name: str) -> Optional[dict]:
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _flatten(source: Optional[dict], target: dict[str, str]) -> None:
    """把 {code: {文本: [...]}} 压平成 {code: 文本}，并统一成大写键。"""
    if not isinstance(source, dict):
        return
    for code, value in source.items():
        if isinstance(value, dict) and value:
            text = next(iter(value.keys()), "")
        elif isinstance(value, str):
            text = value
        else:
            continue
        target[str(code).upper()] = str(text)


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        zh = _load_file("hms_zh_cn.json.gz")
        en = _load_file("hms_en.json.gz")
        for source in (en, zh):  # 中文覆盖英文
            if not source:
                continue
            _flatten(source.get("device_hms"), _hms_text)
            _flatten(source.get("device_error"), _error_text)
        links = _load_file("wiki_links.json.gz")
        if isinstance(links, dict):
            for code, value in links.items():
                if isinstance(value, dict) and value:
                    path = next(iter(value.keys()), "")
                elif isinstance(value, str):
                    path = value
                else:
                    continue
                if path:
                    _wiki_links[str(code).upper()] = str(path)
        _loaded = True


def hms_key(attr: int, code: int) -> str:
    """HMS 组合键：8 位 attr + 8 位 code（大写十六进制）。"""
    try:
        return f"{int(attr) & 0xFFFFFFFF:08X}{int(code) & 0xFFFFFFFF:08X}"
    except (TypeError, ValueError):
        return ""


def hms_text(attr: int, code: int) -> str:
    """HMS 的中文说明（查不到返回空字符串）。"""
    _ensure_loaded()
    return _hms_text.get(hms_key(attr, code), "")


def error_text(code: int) -> str:
    """``print_error`` 的说明文案。"""
    _ensure_loaded()
    try:
        key = f"{int(code) & 0xFFFFFFFF:08X}"
    except (TypeError, ValueError):
        return ""
    return _error_text.get(key, "")


def wiki_url(attr: int, code: int) -> str:
    """官方 wiki 解释页（查不到就按通用规则拼一个）。"""
    _ensure_loaded()
    key = hms_key(attr, code)
    path = _wiki_links.get(key)
    if not path:
        path = f"/en/x1/troubleshooting/hmscode/{key[0:4]}_{key[4:8]}_{key[8:12]}_{key[12:16]}"
    if path.startswith("http"):
        return path
    return WIKI_BASE + path


def display_code(attr: int, code: int) -> str:
    """给用户看的短码，例如 HMS_0300_0100_0001_0001。"""
    key = hms_key(attr, code)
    if len(key) != 16:
        return ""
    return f"HMS_{key[0:4]}_{key[4:8]}_{key[8:12]}_{key[12:16]}"


def data_available() -> bool:
    _ensure_loaded()
    return bool(_hms_text or _error_text)
