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

from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

from app.bambu.models import PrinterInfo, PrinterModel  # noqa: E402
from app.config import AppConfig  # noqa: E402


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


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
    try:
        window.export_config()
    finally:
        window.close()

    assert target.exists(), "导出按钮没有生成文件"
    assert target.stat().st_size > 0


def test_export_config_default_dir_uses_documents(tmp_path, monkeypatch, isolated_config_dir):
    """默认目录应当来自 QStandardPaths（并且确实是 QtCore 里的那个类）。"""
    from PySide6.QtCore import QStandardPaths

    window = _make_window(tmp_path, monkeypatch)
    captured: dict[str, str] = {}

    def fake_dialog(parent, title, directory, filters):
        captured["directory"] = directory
        return "", ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(fake_dialog))
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
    shown: list[str] = []
    monkeypatch.setattr(
        "app.ui.main_window.QMessageBox.warning",
        staticmethod(lambda *args, **kwargs: shown.append("warning")),
    )
    try:
        window.export_config()
    finally:
        window.close()

    assert not bad.exists()
    assert shown == ["warning"], "导出失败时应当弹一次警告"
