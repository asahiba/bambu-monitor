"""审计用户真实配置与当前版本是否对得上（**只读**，不会写回、不打印凭据）。

检查项：

1. 顶层字段：现在代码认识的 vs 文件里实际有的（多出来的 = 老版本遗留，少掉的 = 会走默认值）；
2. 每台设备的字段：未知键、类型不对、取值超出钳制范围（会被**静默**改掉的那些）；
3. `model` 字符串能不能被 `PrinterModel` 认出来 —— 认不出来会退化成"未知机型"，
   而**机型决定视频通道**（未知机型走 auto：先试 RTSPS 再退 6000），画面行为会跟着变；
4. 凭据能不能在本机解开（`dpapi:` / `fernet:` 换了机器/用户就解不开）；
5. 重复设备（同 IP / 同序列号）—— 会出现两条会话、两条画面；
6. `stream_mode` 等用户级覆盖有没有把设备钉在错误的通道上；
7. 真正 `AppConfig.load()` 之后程序看到的值，以及它的 warnings / last_error。
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu.models import PrinterInfo, PrinterModel, detect_model  # noqa: E402
from app.config import AppConfig, config_path  # noqa: E402
from app.core.registry import resolve_family  # noqa: E402
from app.util import secret  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

enable_utf8()

RAW = json.load(open(config_path(), encoding="utf-8"))
KNOWN_TOP = {
    "format", "portable", "printers", "columns", "window_geometry", "show_timestamp",
    "auto_connect", "last_timeout", "max_fps", "refresh_ms", "web_enabled", "web_port",
    "web_token", "web_fps", "web_max_width",
}
FIELDS = {field.name for field in dataclasses.fields(PrinterInfo)}
# 当前代码里的钳制范围（config.py 的 _coerce_* 调用）
RANGES = {
    "columns": (0, 999),
    "last_timeout": (15.0, 60.0),
    "max_fps": (0.0, 30.0),
    "refresh_ms": (50, 1000),
    "web_port": (1, 65535),
    "web_fps": (0.5, 15.0),
    "web_max_width": (240, 1920),
}
PRINTER_RANGES = {"tile_span": (1, 3), "port": (0, 65535), "camera_index": (0, 99)}


def main() -> int:
    print(f"配置文件：{config_path()}")
    print(f"文件大小：{os.path.getsize(config_path()) / 1024:.1f} KB\n")

    print("=== ① 顶层字段 ===")
    extra = sorted(set(RAW) - KNOWN_TOP)
    print(f"  文件里有 {len(RAW)} 个键；代码不认识的老键：{extra or '（无）'}")
    for key, (low, high) in RANGES.items():
        value = RAW.get(key, "（缺失）")
        note = ""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value < low or value > high:
                note = f"  ⚠️ 超出范围 {low}~{high}，会被静默改成边界值"
        elif value == "（缺失）":
            note = "  （缺失 → 用默认值）"
        print(f"  {key:16} = {value!r}{note}")

    printers = RAW.get("printers", [])
    print(f"\n=== ② 每台设备的字段（共 {len(printers)} 台） ===")
    unknown_keys: dict[str, int] = {}
    type_problems: list[str] = []
    for item in printers:
        label = str(item.get("name") or item.get("ip") or "?")
        for key in item:
            if key not in FIELDS:
                unknown_keys[key] = unknown_keys.get(key, 0) + 1
        for key, (low, high) in PRINTER_RANGES.items():
            value = item.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if value < low or value > high:
                    type_problems.append(f"{label}: {key}={value} 超出 {low}~{high}")
        for key in ("ip", "name", "serial", "model", "access_code", "api_key", "camera_url"):
            value = item.get(key)
            if value is not None and not isinstance(value, str):
                type_problems.append(f"{label}: {key} 不是字符串而是 {type(value).__name__}")
    print(f"  代码不认识的老字段：{unknown_keys or '（无）'}")
    print(f"  取值/类型问题：{type_problems or '（无）'}")

    print("\n=== ③ 机型识别（机型决定视频通道） ===")
    model_problems = 0
    for item in printers:
        raw_model = str(item.get("model", ""))
        serial = str(item.get("serial", ""))
        try:
            model = PrinterModel(raw_model)
            recognized = "文件里的值可识别"
        except ValueError:
            model = PrinterModel.UNKNOWN
            recognized = "⚠️ 文件里的值**认不出来** → 退化成未知机型"
            model_problems += 1
        guessed = detect_model(serial, item.get("name", ""))
        family = resolve_family(PrinterInfo(family=str(item.get("family", "")), model=model))
        channel = model.video_channel
        if model is PrinterModel.UNKNOWN and family.family == "moonraker":
            channel = "（第三方族：快照）"
        print(
            f"  {str(item.get('ip', '')):16} model={raw_model!r:12} {recognized}"
            f"  序列号识别={guessed.label!r:10} 族={family.family:9} 通道={channel}"
        )
    print(f"  ⚠️ 认不出机型的设备：{model_problems} 台")

    print("\n=== ④ 凭据能否在本机解开 ===")
    secret.clear_last_error()
    locked = []
    for item in printers:
        code = str(item.get("access_code", ""))
        key = str(item.get("api_key", ""))
        state = []
        for name, value in (("访问代码", code), ("API Key", key)):
            if not value:
                state.append(f"{name}=空")
            elif secret.is_encrypted(value):
                if secret.decrypt_text(value):
                    state.append(f"{name}={value.split(':', 1)[0]}✓可解")
                else:
                    state.append(f"{name}={value.split(':', 1)[0]}✗**解不开**")
                    locked.append(f"{item.get('name') or item.get('ip')} 的{name}")
            else:
                state.append(f"{name}=明文")
        print(f"  {str(item.get('ip', '')):16} {'  '.join(state)}")
    print(f"  解不开的凭据：{locked or '（无）'}")

    print("\n=== ⑤ 重复设备 ===")
    by_ip: dict[str, int] = {}
    by_serial: dict[str, int] = {}
    for item in printers:
        by_ip[str(item.get("ip", ""))] = by_ip.get(str(item.get("ip", "")), 0) + 1
        if item.get("serial"):
            by_serial[str(item["serial"])] = by_serial.get(str(item["serial"]), 0) + 1
    dup_ip = {k: v for k, v in by_ip.items() if v > 1}
    dup_serial = {k: v for k, v in dups.items() if v > 1} if (dups := by_serial) else {}
    print(f"  重复 IP：{dup_ip or '（无）'}")
    print(f"  重复序列号：{dup_serial or '（无）'}")

    print("\n=== ⑥ 用户级通道覆盖（stream_mode） ===")
    for item in printers:
        mode = str(item.get("stream_mode", "auto"))
        if mode.lower() != "auto":
            print(f"  ⚠️ {item.get('ip')} 被钉在 {mode}（用户覆盖优先于机型默认）")
    if all(str(item.get("stream_mode", "auto")).lower() == "auto" for item in printers):
        print("  （全部是 auto，没有覆盖）")

    print("\n=== ⑦ 程序实际加载到的结果 ===")
    config = AppConfig.load()
    print(f"  台数={len(config.printers)}  max_fps={config.max_fps}  refresh_ms={config.refresh_ms}"
          f"  web_fps={config.web_fps}  web_max_width={config.web_max_width}")
    print(f"  last_error = {config.last_error!r}")
    print(f"  warnings   = {config.warnings!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
