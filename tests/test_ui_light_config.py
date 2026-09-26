"""契约：「添加 / 编辑打印机」里的「开灯 / 关灯 G-code」两行按**设备族**生效。

## 为什么让用户填命令

Klipper 机器上「舱灯」没有统一做法 —— 可能是宏（``LIGHT_ON``）、可能是输出引脚
（``SET_PIN PIN=caselight VALUE=1``）、也可能接在某个风扇上
（``SET_FAN_SPEED FAN=chamber_light SPEED=1``）。真机（Voron 2.4）实测 60 个对象里
既没有 ``led`` / ``neopixel`` / ``output_pin``，也没有灯光宏：**猜名字必然踩空**，
而踩空的表现是"点了没反应"，用户会以为软件坏了。所以按 Fluidd / Mainsail 的做法
让用户填**命令**，程序只负责发（``POST /printer/gcode/script``）。

界面这一侧因此必须守住三条：

1. 拓竹族下这两行**停用**（拓竹的灯由机型能力决定，填了不会有任何作用）并有说明；
2. 用户填的命令要**原样**存进 ``PrinterInfo.light_on_gcode`` / ``light_off_gcode``；
3. 留空落成**空字符串**（不是 ``None``、也不是空白串）—— 这是
   「留空 = 这台设备没有可控灯光」在配置里的表示，适配器据此**不声明**
   ``can_control_light``，界面也就不会出现一个按下去没反应的灯按钮。

## 全程离线

对话框只被构造与读写控件：不点「测试连接」（那才会起探测线程），
校验灯控约定时也只**构造**适配器（``start()`` 都没调），一个请求都不发。

写法与同一套约定的 ``tests/test_ui_add_dialog_family.py`` 一致。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

# ⚠️ 必须在导入 PySide6 **之前**判断：CI 刻意不装 PySide6，
# 而模块级的 ``from PySide6... import`` 在无 Qt 时是收集期错误（不是跳过）。
pytest.importorskip("PySide6.QtWidgets", reason="界面测试需要 PySide6（CI 不装它）")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QMessageBox,
)

from app.bambu.models import PrinterInfo  # noqa: E402
from app.core import registry  # noqa: E402
from app.ui.add_dialog import PrinterEditDialog  # noqa: E402

FAMILY_BAMBU = registry.FAMILY_BAMBU
FAMILY_MOONRAKER = registry.FAMILY_MOONRAKER

#: 真机（Voron 2.4）上最可能的两种接法，测试里当"用户会填什么"的代表。
GCODE_ON = "SET_PIN PIN=caselight VALUE=1"
GCODE_OFF = "SET_PIN PIN=caselight VALUE=0"


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def dialog_factory(monkeypatch, isolated_config_dir):
    """造对话框，并把所有模态弹窗挡掉。

    ``_accept()`` 在缺 IP 时会弹 ``QMessageBox.warning``（模态）：offscreen 下没人点，
    pytest 会永久卡住。这里替换成记录调用，测试还顺便能断言"该弹的提示弹了"。
    """
    _app()
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *args, **kwargs: warnings.append(args[2]))
    )
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    created: list[PrinterEditDialog] = []

    def build(printer=None) -> PrinterEditDialog:
        dialog = PrinterEditDialog(None, printer=printer)
        created.append(dialog)
        return dialog

    build.warnings = warnings  # type: ignore[attr-defined]
    yield build
    for dialog in created:
        dialog.close()


def _select_family(dialog: PrinterEditDialog, family: str) -> None:
    """像用户那样切设备族（走 currentIndexChanged，而不是直接改内部状态）。"""
    index = dialog.family_combo.findData(family)
    assert index >= 0, f"设备族下拉里没有 {family}"
    dialog.family_combo.setCurrentIndex(index)


def _moonraker_dialog(dialog_factory) -> PrinterEditDialog:
    """造一个已填好 IP 的 Moonraker 对话框（灯控两行应当可用）。"""
    dialog = dialog_factory()
    dialog.ip_edit.setText("192.168.1.77")
    _select_family(dialog, FAMILY_MOONRAKER)
    return dialog


# ------------------------------------------------------------------ ① 按族启用 / 停用


def test_拓竹族下灯控两行被停用并给出说明(dialog_factory):
    """拓竹的灯由机型能力决定（P1S 有、A1 没有），填命令没有任何作用 → 停用。

    必须同时给一句说明：只把行置灰而不解释，用户会以为界面坏了，
    或者以为"拓竹这台机器的灯永远控制不了"。
    """
    dialog = dialog_factory()

    assert not dialog.light_on_edit.isEnabled(), "拓竹族下「开灯 G-code」不该可填"
    assert not dialog.light_off_edit.isEnabled(), "拓竹族下「关灯 G-code」不该可填"
    assert not dialog.light_on_label.isEnabled()
    assert not dialog.light_off_label.isEnabled()
    # 停用而不是隐藏：用户仍能看见这一行存在（也就知道第三方设备上有这么个开关）
    assert dialog.light_note.isVisibleTo(dialog), "停用时要说明原因"
    note = dialog.light_note.text()
    assert "停用" in note, f"说明里没写清这两行被停用了：{note}"
    assert "拓竹" in note, f"说明里没说清这是拓竹族的限制：{note}"


def test_切到第三方族后灯控两行可用切回拓竹又停用(dialog_factory):
    """灯控是第三方专有：切到 Moonraker 立刻可用，切回拓竹立刻恢复停用。"""
    dialog = dialog_factory()
    assert not dialog.light_on_edit.isEnabled()

    _select_family(dialog, FAMILY_MOONRAKER)
    assert dialog.light_on_edit.isEnabled(), "Moonraker 上必须能填灯命令"
    assert dialog.light_off_edit.isEnabled()
    assert dialog.light_on_label.isEnabled() and dialog.light_off_label.isEnabled()
    note = dialog.light_note.text()
    assert "留空" in note, f"第三方族要说清'留空'是什么意思：{note}"

    _select_family(dialog, FAMILY_BAMBU)
    assert not dialog.light_on_edit.isEnabled(), "切回拓竹后应当恢复停用"
    assert not dialog.light_off_edit.isEnabled()


@pytest.mark.parametrize(
    "attr",
    ["light_on_edit", "light_off_edit"],
)
def test_占位提示给出示例并说明留空的含义(dialog_factory, attr):
    """提示必须做到两件事：给出可照抄的例子，并说清留空 = 没有可控灯光。"""
    hint = getattr(dialog_factory(), attr).placeholderText()
    assert "例如" in hint, f"提示里没有例子：{hint}"
    # 宏与输出引脚这两类最常见接法至少各给一个（风扇那类写在行下方的说明里）
    assert ("LIGHT_ON" in hint) or ("LIGHT_OFF" in hint), f"没有宏的例子：{hint}"
    assert "SET_PIN PIN=caselight" in hint, f"没有输出引脚的例子：{hint}"
    assert "留空" in hint, f"没说清留空是什么意思：{hint}"
    assert "没有可控灯光" in hint, f"留空的含义说得不够明确：{hint}"


def test_两行的标题与字段名对得上(dialog_factory):
    """标签必须是用户看得懂的「开灯 / 关灯 G-code」（配置字段名不进界面）。"""
    dialog = dialog_factory()
    assert dialog.light_on_label.text() == "开灯 G-code"
    assert dialog.light_off_label.text() == "关灯 G-code"


# --------------------------------------------------------------------------- ② 保存


def test_填写的命令原样存进_light_on_gcode_与_light_off_gcode(dialog_factory):
    """契约：填什么存什么 —— 界面不做任何"聪明"的改写（不猜名字、不加前缀）。"""
    dialog = _moonraker_dialog(dialog_factory)
    dialog.light_on_edit.setText(GCODE_ON)
    dialog.light_off_edit.setText(GCODE_OFF)

    dialog._accept()
    info = dialog.result
    assert info is not None, "填了 IP 就该保存成功"
    assert info.family == FAMILY_MOONRAKER
    assert info.light_on_gcode == GCODE_ON
    assert info.light_off_gcode == GCODE_OFF
    # 这两个字段是第三方族专有的：不能顺手串到拓竹的字段上
    assert info.access_code == "" and info.api_key == ""


def test_用户填的宏名也原样保存(dialog_factory):
    """宏这一路（LIGHT_ON / LIGHT_OFF）同样原样存，界面不挑写法。"""
    dialog = _moonraker_dialog(dialog_factory)
    dialog.light_on_edit.setText("LIGHT_ON")
    dialog.light_off_edit.setText("LIGHT_OFF")

    dialog._accept()
    assert (dialog.result.light_on_gcode, dialog.result.light_off_gcode) == (
        "LIGHT_ON",
        "LIGHT_OFF",
    )


@pytest.mark.parametrize("text", ["", "   ", "\t "])
def test_留空落成空字符串而不是_None_或空白(dialog_factory, text):
    """留空 = 明确声明"这台设备没有可控灯光"，配置里必须是干净的 ``""``。

    写成 ``None`` 或 ``"   "`` 都会让下游的判断（``bool(...)``）出现两种不一致的
    假值，而 ``asdict`` 写出的 JSON 里还会多出一个空白串 —— 用户看到会以为填过东西。
    """
    dialog = _moonraker_dialog(dialog_factory)
    dialog.light_on_edit.setText(text)
    dialog.light_off_edit.setText(text)

    dialog._accept()
    info = dialog.result
    assert info is not None
    assert info.light_on_gcode == "" and isinstance(info.light_on_gcode, str)
    assert info.light_off_gcode == "" and isinstance(info.light_off_gcode, str)
    assert not info.light_on_gcode and not info.light_off_gcode


def test_命令两端的空白被去掉(dialog_factory):
    """用户从别处复制命令时经常带空格/换行缩进，存进去前要清理掉。"""
    dialog = _moonraker_dialog(dialog_factory)
    dialog.light_on_edit.setText(f"  {GCODE_ON}  ")
    dialog.light_off_edit.setText(f" {GCODE_OFF} ")

    dialog._accept()
    assert dialog.result.light_on_gcode == GCODE_ON
    assert dialog.result.light_off_gcode == GCODE_OFF


def test_留空即不声明灯控能力而填了就声明(dialog_factory):
    """「留空 = 没有可控灯光」这条约定的**实际后果**：桌面/网页端的灯按钮出不出来。

    直接拿对话框产出的 ``PrinterInfo`` 去建会话（只构造、不 ``start()``，不发请求），
    这样钉住的是"界面填的东西真的会让灯按钮出现 / 不出现"，而不只是字段名对不对。
    """
    empty = _moonraker_dialog(dialog_factory)
    empty._accept()
    assert empty.result is not None
    assert registry.create_session(empty.result).capabilities.can_control_light is False, (
        "留空了却仍然声明灯控能力 → 界面会出现一个按下去没反应的灯按钮"
    )

    filled = _moonraker_dialog(dialog_factory)
    filled.light_on_edit.setText("LIGHT_ON")
    filled.light_off_edit.setText("LIGHT_OFF")
    filled._accept()
    assert filled.result is not None
    session = registry.create_session(filled.result)
    assert session.capabilities.can_control_light is True, "填了命令就该出现灯按钮"
    # 下发时用的就是用户填的命令原文（灯控走 POST /printer/gcode/script）
    assert (session.light_on_gcode, session.light_off_gcode) == ("LIGHT_ON", "LIGHT_OFF")


def test_只填开灯也声明灯控能力(dialog_factory):
    """只填一半（例如只有 ``LIGHT_ON`` 宏）也算声明了灯控，不能静默丢掉。"""
    dialog = _moonraker_dialog(dialog_factory)
    dialog.light_on_edit.setText("LIGHT_ON")

    dialog._accept()
    info = dialog.result
    assert info is not None
    assert (info.light_on_gcode, info.light_off_gcode) == ("LIGHT_ON", "")
    assert registry.create_session(info).capabilities.can_control_light is True


def test_拓竹设备保存时不会凭空多出灯命令(dialog_factory):
    """拓竹族下这两行是停用的，新建一台拓竹设备不该在配置里写出灯命令字段值。"""
    dialog = dialog_factory()
    dialog.ip_edit.setText("192.168.1.50")
    dialog.code_edit.setText("12345678")

    dialog._accept()
    info = dialog.result
    assert info is not None
    assert info.family == "", "拓竹写空 family（老配置的约定）"
    assert info.light_on_gcode == "" and info.light_off_gcode == ""


# --------------------------------------------------------------------------- ③ 回填


def test_编辑既有_moonraker_设备时回填已填的命令(dialog_factory):
    """回填：编辑一台已保存的 Moonraker 设备（Voron 2.4），两行里应当是原来的命令。"""
    printer = PrinterInfo(
        ip="192.168.1.88",
        name="车间 Voron 2.4",
        family=FAMILY_MOONRAKER,
        light_on_gcode=GCODE_ON,
        light_off_gcode=GCODE_OFF,
    )
    dialog = dialog_factory(printer)
    assert dialog.family_combo.currentData() == FAMILY_MOONRAKER
    assert dialog.light_on_edit.text() == GCODE_ON, "开灯命令没回填"
    assert dialog.light_off_edit.text() == GCODE_OFF, "关灯命令没回填"
    assert dialog.light_on_edit.isEnabled() and dialog.light_off_edit.isEnabled()

    # 什么也不改直接保存：命令必须还在（不能"编辑一次就丢配置"）
    dialog._accept()
    assert dialog.result is not None
    assert dialog.result.light_on_gcode == GCODE_ON
    assert dialog.result.light_off_gcode == GCODE_OFF


def test_编辑既有_moonraker_设备且没填过灯命令时两行为空(dialog_factory):
    """老设备（没填过灯命令）编辑时两行是空的，保存后仍是空字符串。"""
    printer = PrinterInfo(ip="192.168.1.88", family=FAMILY_MOONRAKER)
    dialog = dialog_factory(printer)
    assert dialog.light_on_edit.text() == ""
    assert dialog.light_off_edit.text() == ""

    dialog._accept()
    assert dialog.result.light_on_gcode == ""
    assert dialog.result.light_off_gcode == ""


def test_把设备族改成拓竹时不会静默丢配置里的灯命令(dialog_factory):
    """拓竹族下这两行是停用的 → 取值沿用原配置，而不是"编辑一次就抹成空"。

    与同为停用行的 ``serial`` / ``model`` / ``stream_mode`` 保持同一约定：停用的行
    不参与保存逻辑。字段在拓竹设备上是惰性的（拓竹会话根本不读它），
    但保留下来意味着用户万一改回第三方族，命令还在。
    """
    printer = PrinterInfo(
        ip="192.168.1.88",
        family=FAMILY_MOONRAKER,
        light_on_gcode=GCODE_ON,
        light_off_gcode=GCODE_OFF,
    )
    dialog = dialog_factory(printer)
    _select_family(dialog, FAMILY_BAMBU)
    assert not dialog.light_on_edit.isEnabled()

    dialog._accept()
    info = dialog.result
    assert info is not None and info.family == ""
    assert info.light_on_gcode == GCODE_ON, "停用行不该把用户旧值抹掉"
    assert info.light_off_gcode == GCODE_OFF


def test_切族来回切换不会把灯命令弄丢或串进拓竹(dialog_factory):
    """用户在对话框里来回切族（看看两边都要填什么）时，值不能互相冲掉。"""
    dialog = _moonraker_dialog(dialog_factory)
    dialog.light_on_edit.setText(GCODE_ON)
    dialog.light_off_edit.setText(GCODE_OFF)

    _select_family(dialog, FAMILY_BAMBU)
    assert not dialog.light_on_edit.isEnabled()
    assert dialog.light_on_edit.text() == GCODE_ON, "切到拓竹时值不该被抹掉（切回来还要用）"

    _select_family(dialog, FAMILY_MOONRAKER)
    assert dialog.light_on_edit.text() == GCODE_ON, "切回第三方族后命令丢了"

    dialog._accept()
    assert (dialog.result.light_on_gcode, dialog.result.light_off_gcode) == (
        GCODE_ON,
        GCODE_OFF,
    )
