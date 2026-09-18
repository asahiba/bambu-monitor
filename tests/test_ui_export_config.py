"""契约：GUI 里的「导出 / 导入配置」按钮必须真的能用。

为什么单独立一条：``app/config.py`` 的 ``export_to`` / ``import_from`` 早就被测过，
但**界面按钮走的是另一段代码**（取默认目录 + 弹文件对话框），
那段里曾经写过 ``from PySide6.QtWidgets import QStandardPaths`` —— 这个导入
在 pyflakes/ruff 里都看不出来（模块存在、只是名字不在里面），
只有真的点一下按钮才会炸：

    cannot import name 'QStandardPaths' from 'PySide6.QtWidgets'

所以这里用 offscreen 平台建一个真实主窗口，把文件对话框替换掉，直接调用按钮的槽函数。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox  # noqa: E402

from app.bambu.models import PrinterInfo, PrinterModel  # noqa: E402
from app.config import AppConfig  # noqa: E402


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _stub_passphrase_prompt(monkeypatch, value: str = "") -> list[str]:
    """把「导出/导入配置」的口令输入框替换成固定答案。

    ⚠️ 不替换的话测试会**卡住不动**：`QInputDialog.getText` 是模态的，
    在 offscreen 下没人能点确定。返回的列表会记下每次被问到的提示语。
    """
    asked: list[str] = []

    def fake_get_text(parent, title, label, *args, **kwargs):  # noqa: ANN001
        asked.append(label)
        return value, True

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(fake_get_text))
    return asked


def _stub_message_boxes(monkeypatch) -> list[tuple[str, str]]:
    """把消息框换成记录器（同样是模态的，不替换就会卡住）。

    返回 ``[(标题, 正文), …]``。导出成功后界面会弹一个说明框（告诉用户这份文件
    能不能拿到别的设备上用），所以凡是要走导出路径的测试都必须先替换掉它。
    """
    seen: list[tuple[str, str]] = []

    def fake_information(parent, title, text, *args, **kwargs):  # noqa: ANN001
        seen.append((str(title), str(text)))
        return QMessageBox.Ok

    def fake_warning(parent, title, text, *args, **kwargs):  # noqa: ANN001
        seen.append((str(title), str(text)))
        return QMessageBox.Ok

    monkeypatch.setattr(QMessageBox, "information", staticmethod(fake_information))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(fake_warning))
    return seen


def _make_window(tmp_path, monkeypatch):
    """建一个不联网、不接打印机的主窗口（会话为未启动状态）。"""
    _app()
    from app.ui.main_window import MainWindow

    config = AppConfig()
    config.persist = False  # 绝不写真实配置
    config.auto_connect = False
    config.printers = [
        PrinterInfo(
            ip=f"10.0.0.{index + 10}",
            name=f"测试机{index + 1}",
            serial=f"TESTSERIAL{index:04d}",
            model=PrinterModel.P1S,
            access_code=f"1234567{index}",
        )
        for index in range(3)
    ]
    return MainWindow(config)


def test_export_config_button_writes_file(tmp_path, monkeypatch, isolated_config_dir):
    """点「导出配置」应当弹出保存框并写出文件，而不是抛 ImportError。"""
    window = _make_window(tmp_path, monkeypatch)
    target = tmp_path / "exported.json"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: (str(target), "JSON 文件 (*.json)")),
    )
    _stub_passphrase_prompt(monkeypatch)  # 口令留空 = 旧的「只在本机可恢复」导出
    _stub_message_boxes(monkeypatch)
    try:
        window.export_config()
    finally:
        window.close()

    assert target.exists(), "导出按钮没有生成文件"
    assert target.stat().st_size > 0


def test_export_config_带口令导出可在别的机器导入(tmp_path, monkeypatch, isolated_config_dir):
    """回归：界面上「输入口令」导出的文件，必须能在一台**没有本机密钥**的机器上导入。

    这是「导出的配置文件在所有版本都可用」的界面入口：不带口令时访问代码按本机方式
    加密（Windows 是 DPAPI，绑定当前用户），换到安卓/别的电脑就解不开。
    """
    window = _make_window(tmp_path, monkeypatch)
    target = tmp_path / "portable.json"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: (str(target), "JSON 文件 (*.json)")),
    )
    asked = _stub_passphrase_prompt(monkeypatch, "我的口令")
    _stub_message_boxes(monkeypatch)
    try:
        window.export_config()
    finally:
        window.close()

    assert len(asked) == 2, "应当问两次（输入 + 确认）"
    raw = target.read_text(encoding="utf-8")
    assert "12345670" not in raw, "带口令导出时不得出现明文访问代码"
    assert "bmp1:" in raw

    # 模拟「换一台机器」：新配置对象 + 显式口令导入（中间不碰本机密钥）
    fresh = AppConfig()
    assert AppConfig.needs_passphrase(str(target)) is True
    assert fresh.import_from(str(target), "我的口令") is True
    assert [info.access_code for info in fresh.printers] == ["12345670", "12345671", "12345672"]

    # 没给口令 / 口令错都必须失败，并且给出能看懂的原因
    assert AppConfig().import_from(str(target)) is False
    wrong = AppConfig()
    assert wrong.import_from(str(target), "错口令") is False
    assert "口令" in wrong.last_error


def test_export_config_default_dir_uses_documents(tmp_path, monkeypatch, isolated_config_dir):
    """默认目录应当来自 QStandardPaths（并且确实是 QtCore 里的那个类）。"""
    from PySide6.QtCore import QStandardPaths

    window = _make_window(tmp_path, monkeypatch)
    captured: dict[str, str] = {}

    def fake_dialog(parent, title, directory, filters):
        captured["directory"] = directory
        return "", ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(fake_dialog))
    _stub_passphrase_prompt(monkeypatch)  # 口令留空
    _stub_message_boxes(monkeypatch)
    try:
        window.export_config()
    finally:
        window.close()

    expected = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
    assert captured.get("directory"), "没有传出默认目录"
    if expected:
        assert captured["directory"].startswith(expected), (
            f"默认目录不是「文档」：{captured['directory']}"
        )
    assert captured["directory"].endswith("bambu-monitor-config.json")


def test_import_config_button_restores_printers(tmp_path, monkeypatch, isolated_config_dir):
    """点「导入配置」应当把文件里的打印机恢复到监控墙。"""
    window = _make_window(tmp_path, monkeypatch)
    exported = tmp_path / "roundtrip.json"
    window.config.export_to(str(exported))

    # 清空监控墙，模拟「换一台电脑导入」
    for session in list(window.sessions):
        session.stop()
    for tile in list(window.tiles):
        tile.shutdown()
        tile.setParent(None)
        tile.deleteLater()
    window.sessions.clear()
    window.tiles.clear()

    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *args, **kwargs: (str(exported), "JSON 文件 (*.json)")),
    )
    try:
        window.import_config()
        restored = len(window.tiles)
    finally:
        window.close()

    assert restored == 3, f"导入后应当恢复 3 台，实际 {restored} 台"


def test_export_config_reports_failure_without_crashing(tmp_path, monkeypatch, isolated_config_dir):
    """写不进去时应当提示失败，而不是抛异常。"""
    window = _make_window(tmp_path, monkeypatch)
    bad = tmp_path / "不存在的目录" / "x.json"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: (str(bad), "JSON 文件 (*.json)")),
    )
    _stub_passphrase_prompt(monkeypatch)
    boxes = _stub_message_boxes(monkeypatch)
    try:
        window.export_config()
    finally:
        window.close()

    assert not bad.exists()
    assert boxes and "导出失败" in boxes[0][0], "导出失败时应当弹一次警告"


def test_import_config_prompts_passphrase_for_portable_file(tmp_path, monkeypatch, isolated_config_dir):
    """导入带口令的文件时，界面要先问口令；口令对了才真的导入。"""
    source = _make_window(tmp_path, monkeypatch)
    portable = tmp_path / "portable.json"
    source.config.export_to(str(portable), "共享口令")
    source.close()

    window = _make_window(tmp_path, monkeypatch)
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *args, **kwargs: (str(portable), "JSON 文件 (*.json)")),
    )
    asked = _stub_passphrase_prompt(monkeypatch, "共享口令")
    _stub_message_boxes(monkeypatch)
    try:
        window.import_config()
    finally:
        window.close()

    assert asked, "导入带口令的文件时必须问一次口令"
    assert window.config.printers, "口令正确时应当导入成功"
