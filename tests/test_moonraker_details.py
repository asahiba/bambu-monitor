"""Moonraker「详细读数」的契约测试（`parse_details`）。

## 这一版加了什么

用户的原话是「当前打印信息不全」。Klipper 能给的远不止进度/温度：风扇转速、
断料/走料传感器、工具头板温度、主机负载、MCU 固件、耗材用量、屏幕提示、实时位置、
速度/流量倍率……这些字段名各家不同，塞进通用状态模型会把模型撑爆，
所以走 `details()` 这条**展示用**的通路：`[{"label": …, "value": …}, …]`。

## 为什么每条都要单测

这些读数直接显示给用户，**错一个就是假警报**。真机上当场踩到两个：

* 断料开关的 `filament_detected: true` 表示**有料**，第一版写反了，
  打印机正常打印时界面显示「无料」；
* 走料检测（`filament_motion_sensor`）给的是**瞬时**值，挤出间隙读到 false 很正常，
  第一版把它翻译成「可能堵料」——同样是正常打印时报出来的假警报。

原始数据（Voron 2.4 实测）：`filament_switch_sensor 断料监测: {filament_detected: true,
enabled: true}`、`filament_motion_sensor 转堵监测: {filament_detected: false, enabled: true}`。
"""

from __future__ import annotations

from app.adapters.moonraker.adapter import parse_details, parse_status


def _labels(rows: list[dict[str, str]]) -> list[str]:
    return [row["label"] for row in rows]


def _value(rows: list[dict[str, str]], label: str) -> str:
    for row in rows:
        if row["label"] == label:
            return row["value"]
    return ""


# ------------------------------------------------------------------ 传感器语义
def test_断料开关有料时不能说成无料():
    """**回归**：`filament_detected: true` = 有料（真机正在打印时就是这个值）。"""
    rows = parse_details(
        {"result": {"status": {"filament_switch_sensor 断料监测": {"filament_detected": True}}}}
    )
    assert _value(rows, "断料检测 断料监测") == "有料"


def test_断料开关触发时才说无料():
    rows = parse_details(
        {"result": {"status": {"filament_switch_sensor runout": {"filament_detected": False}}}}
    )
    assert "无料" in _value(rows, "断料检测 runout")


def test_走料检测不下堵料结论():
    """**回归**：走料检测是瞬时值，读到 false 不能报「可能堵料」。"""
    rows = parse_details(
        {"result": {"status": {"filament_motion_sensor 转堵监测": {"filament_detected": False}}}}
    )
    text = _value(rows, "走料检测 转堵监测")
    assert "堵料" not in text, f"不该由瞬时值下堵料结论：{text!r}"
    assert "瞬时" in text, "要说明这是瞬时值，否则用户会当成故障"


def test_走料检测有走料时说检测到():
    rows = parse_details(
        {"result": {"status": {"filament_motion_sensor m": {"filament_detected": True}}}}
    )
    assert _value(rows, "走料检测 m") == "检测到走料"


# ------------------------------------------------------------------ 其余读数
def test_风扇按设备实际名字列出():
    """风扇对象名由设备配置决定（fan / heater_fan xxx），要原样列出并带转速。"""
    rows = parse_details(
        {
            "result": {
                "status": {
                    "fan": {"speed": 1.0, "rpm": None},
                    "heater_fan hotend_fan": {"speed": 0.5},
                    "heater_fan fan2": {"speed": 0.7, "rpm": 1234.0},
                }
            }
        }
    )
    assert _value(rows, "风扇 fan") == "100%"
    assert _value(rows, "风扇 hotend_fan") == "50%"
    assert "1234 rpm" in _value(rows, "风扇 fan2")


def test_打印时长与耗材用量():
    rows = parse_details(
        {
            "result": {
                "status": {
                    "print_stats": {
                        "print_duration": 4020.0,
                        "total_duration": 4320.0,
                        "filament_used": 3860.0,
                    }
                }
            }
        }
    )
    assert _value(rows, "已打印") == "1小时07分"
    assert _value(rows, "累计时长") == "1小时12分"
    assert _value(rows, "耗材用量") == "3.86 m"


def test_文件位置与倍率():
    rows = parse_details(
        {
            "result": {
                "status": {
                    "virtual_sdcard": {"file_position": 6912000, "file_size": 29884416},
                    "gcode_move": {"speed_factor": 1.0, "extrude_factor": 0.95},
                }
            }
        }
    )
    assert _value(rows, "文件位置") == "6.6 / 28.5 MB"
    assert _value(rows, "倍率") == "速度 100% · 流量 95%"


def test_位置与归零状态():
    rows = parse_details(
        {
            "result": {
                "status": {
                    "toolhead": {
                        "position": [168.4, 133.3, 6.6, 0.0],
                        "homed_axes": "xyz",
                    }
                }
            }
        }
    )
    assert _value(rows, "当前位置") == "X 168.4  Y 133.3  Z 6.6"
    assert _value(rows, "归零状态") == "XYZ 已归零"


def test_没归零时说清哪个轴():
    rows = parse_details({"result": {"status": {"toolhead": {"homed_axes": "x"}}}})
    assert "Y/Z" in _value(rows, "归零状态")


def test_主机与MCU信息():
    rows = parse_details(
        {
            "result": {
                "status": {
                    "system_stats": {"sysload": 1.21, "memavail": 7077920, "memtotal": 8000000},
                    "mcu": {"mcu_version": "v0.13.0-770-gce7002bed"},
                }
            }
        }
    )
    assert "负载 1.21" in _value(rows, "主机")
    assert "内存 12%" in _value(rows, "主机")
    assert _value(rows, "MCU 固件") == "v0.13.0"


def test_工具头板温度与加热功率():
    rows = parse_details(
        {
            "result": {
                "status": {
                    "temperature_sensor EBBCan": {"temperature": 42.4},
                    "extruder": {"power": 0.15, "can_extrude": False, "pressure_advance": 0.037},
                    "heater_bed": {"power": 0.08},
                }
            }
        }
    )
    assert _value(rows, "温度 EBBCan") == "42.4℃"
    assert _value(rows, "喷嘴加热") == "15%"
    assert _value(rows, "热床加热") == "8%"
    assert "不可挤丝" in _value(rows, "挤丝")


def test_缺字段时少一行而不是报错():
    """契约：设备没上报的东西不显示，也不能抛异常（各家配置差很多）。"""
    assert parse_details({}) == []
    assert parse_details({"result": {"status": {}}}) == []
    assert parse_details({"result": {"status": {"fan": {"speed": None}}}}) == []
    # 结构不对也不能炸
    assert parse_details(None) == []  # type: ignore[arg-type]
    assert parse_details({"result": "nope"}) == []


def test_输出引脚会列出当前值():
    """契约：``output_pin``/``led`` 这类输出会列出来（Vorón 的舱灯通常是 output_pin）。"""
    rows = parse_details({"result": {"status": {"output_pin caselight": {"value": 1.0}}}})
    label = next((item for item in _labels(rows) if "caselight" in item), "")
    assert label, f"输出引脚该出现在读数里：{_labels(rows)}"
    assert _value(rows, label) == "1.0"


# ------------------------------------------------------------------ 剩余时间
def test_用已打印时长和进度外推剩余时间():
    """契约：Klipper 没有剩余时间字段，用 print_duration ÷ 进度 外推。"""
    fields = parse_status(
        {
            "result": {
                "status": {
                    "print_stats": {"state": "printing", "print_duration": 4020.0},
                    "virtual_sdcard": {"progress": 0.23},
                }
            }
        }
    )
    # 67 分钟打了 23% -> 总时长约 291 分钟，剩约 224 分钟（3 小时 44 分）
    assert 220 <= fields["remaining_minutes"] <= 228, fields


def test_进度太小或没有时长时不猜剩余时间():
    """契约：刚开始打印时宁可显示"--"，也不显示一个离谱的倒计时。"""
    for progress, duration in ((0.01, 60.0), (0.5, None), (0.0, 600.0)):
        stats: dict = {"state": "printing"}
        if duration is not None:
            stats["print_duration"] = duration
        fields = parse_status(
            {
                "result": {
                    "status": {
                        "print_stats": stats,
                        "virtual_sdcard": {"progress": progress},
                    }
                }
            }
        )
        assert "remaining_minutes" not in fields, (progress, duration)
