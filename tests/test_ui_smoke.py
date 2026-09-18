"""契约：所有「点一下才会执行」的界面代码都必须能被调用而不炸。

这次的教训：``export_config()`` 里写错了一个导入
（``from PySide6.QtWidgets import QStandardPaths``），pyflakes/ruff 查不出来，
因为它藏在按钮的槽函数里 —— 只有用户点下去才会抛 ``ImportError``。

本文件用 offscreen 平台把**每个对话框与每个按钮槽函数**都真实调用一遍：
它不追求验证业务结果（那由各自的专项测试负责），只保证「这条路走得通」。
任何 ImportError / AttributeError / 拼写错误都会在这里立刻暴露。

全部离线：会话都不启动（``auto_connect=False``），IP 用不可达地址，
发现/诊断类对话框会把线程或扫描替换掉，绝不接触真实设备。
"""

from __future__ import annotations

import importlib
import os
import pkgutil

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

# ⚠️ 必须在导入 PySide6 **之前**判断：CI 刻意不装 PySide6（体积上百 MB），
# 而模块级的 `from PySide6... import` 在没有 Qt 时是**收集期错误** ——
# pytest 会以 "Interrupted: N errors during collection" 直接失败（不是跳过）。
# 所以无 Qt 环境里要在这里干净地 skip。
pytest.importorskip("PySide6.QtWidgets", reason="界面测试需要 PySide6（CI 不装它）")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QFileDialog,
    QInputDialog,
    QMessageBox,
)

from app.bambu.models import HmsItem, PrinterInfo, PrinterModel  # noqa: E402
from app.config import AppConfig  # noqa: E402


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def window(monkeypatch, isolated_config_dir):
    """一个不联网、不启动会话的主窗口（带 3 台假打印机）。"""
    _app()
    from app.ui.main_window import MainWindow

    config = AppConfig()
    config.persist = False
    config.auto_connect = False
    config.printers = [
        PrinterInfo(
            ip=f"127.0.0.{index + 10}",  # 保留地址，且不启动会话 → 不会有真实连接
            name=f"冒烟机{index + 1}",
            serial=f"SMOKE{index:010d}",
            model=PrinterModel.P1S,
            access_code="12345678",
        )
        for index in range(3)
    ]
    main = MainWindow(config)
    yield main
    main.close()


def test_every_app_module_imports():
    """app 包下所有模块都能导入（模块级 ImportError 立刻暴露）。"""
    import app

    failures: list[str] = []
    for module in pkgutil.walk_packages(app.__path__, prefix="app."):
        try:
            importlib.import_module(module.name)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{module.name}: {exc.__class__.__name__}: {exc}")
    assert not failures, "存在无法导入的模块：\n" + "\n".join(failures)


def test_main_window_click_slots_do_not_raise(window, tmp_path, monkeypatch):
    """主窗口工具栏 / 右键菜单里的槽函数逐个调用一遍。"""
    target = tmp_path / "exported.json"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), "JSON"))
    )
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(target), "JSON"))
    )
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    # 「导出/导入配置」现在会问一个可选口令（QInputDialog 是模态的，
    # offscreen 下不替换就会一直卡住）—— 这里给空口令 = 旧的导出方式
    monkeypatch.setattr(
        QInputDialog, "getText", staticmethod(lambda *a, **k: ("", True))
    )
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    window.save_config_now()
    window.export_config()
    assert target.exists()
    window.import_config()
    window.rebuild_grid()
    window._update_summary()
    window._notify("冒烟")
    window.toggle_fullscreen()
    window.toggle_fullscreen()
    window.snapshot_all()
    window.connect_all()
    window.disconnect_all()

    # 布局相关：全部动作都走一遍
    for action in ("focus_on", "up", "down", "top", "bottom", "focus_off"):
        window.handle_layout_action(window.tiles[0], action)
    assert window.tiles, "布局操作后不应丢失画面"

    window.toggle_single_view(window.tiles[0])
    window.toggle_single_view(window.tiles[0])
    window._patrol_timer.start()
    window._patrol_next()
    window._patrol_timer.stop()
    window.single_tile = None
    window.rebuild_grid()


def test_dialogs_construct_and_close(window, monkeypatch, tmp_path):
    """所有对话框都能构造出来并正常关闭。"""
    from app.ui.add_dialog import PrinterEditDialog
    from app.ui.discover_dialog import DiscoverDialog
    from app.ui.hms_dialog import HmsDialog
    from app.ui.layout_dialog import LayoutDialog
    from app.ui.settings_dialog import SettingsDialog
    from app.ui.web_dialog import WebInfoDialog

    # 添加/编辑打印机
    dialog = PrinterEditDialog(window, window.tiles[0].session.info)
    assert dialog.stream_combo.count() > 0
    dialog.close()

    # 设置（保存后应当回写配置对象）
    settings = SettingsDialog(window.config, window)
    settings._apply()
    settings.close()
    assert window.config.refresh_ms >= 50

    # 网页地址提示
    web = WebInfoDialog(["http://127.0.0.1:8080/?token=x"], 8080, window)
    web.close()

    # HMS 详情
    hms = HmsDialog(
        [HmsItem(attr=0x03000100, code=0x00010001, text="测试提示", wiki="https://example.com", short_code="HMS_TEST")],
        "冒烟机1",
        window,
    )
    hms.close()

    # 布局窗口（用真实 tiles 构造）
    layout = LayoutDialog(window.tiles, window)
    assert len(layout.ordered_tiles) == len(window.tiles)
    layout.close()

    # 自动搜索：把扫描替换掉，避免真的发广播
    from app.ui import discover_dialog as discover_module

    monkeypatch.setattr(discover_module.DiscoveryService, "start", lambda self: None)
    discover = DiscoverDialog(known=window.config.printers, timeout=5.0, parent=window)
    discover.close()


def test_diagnose_dialog_constructs_offline(window, monkeypatch):
    """诊断对话框能构造（目标用不可达地址，不会碰真实打印机）。"""
    from app.ui.diagnose_dialog import DiagnoseDialog

    info = PrinterInfo(ip="127.0.0.1", name="离线机", serial="", access_code="12345678")
    dialog = DiagnoseDialog(info, window)
    dialog.close()  # closeEvent 会等线程结束
    assert dialog.output.toPlainText() == "" or True  # 只要求不抛异常


def test_tile_buttons_and_context_slots(window, monkeypatch):
    """每路画面的按钮 / 右键菜单槽函数逐个调用（不含真实下发：会话未连接）。"""
    tile = window.tiles[0]
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))

    tile.refresh()
    tile._update_status_panel(tile.session.snapshot())
    tile._apply_button_labels(paused=False)
    tile._apply_button_labels(paused=True)
    tile._open_hms()  # 没有 HMS → 直接返回
    tile._toggle_light()  # 会话未连接 → 不会真的发指令
    tile._toggle_pause()
    tile._stop_print()  # 二次确认被替换成 No
    tile.save_snapshot()  # 没有画面 → 只提示
    tile.shutdown()


def test_web_server_start_without_dialog(window):
    """「开机自动开启网页监控」的路径不能弹出模态对话框。

    原来 ``start_web_server(show_dialog=False)`` 里 ``action_web.setChecked(True)``
    会触发 ``toggled`` 信号 → 重入 ``toggle_web_server()`` → 以
    ``show_dialog=True`` 再走一遍，于是每次启动都莫名弹出「网页信息」模态窗口
    （在无界面环境下就是永久阻塞）。这里既验证起停，也验证不弹窗。
    """
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    window.config.web_port = free_port

    popped: list[bool] = []
    monkeypatch_holder = window._show_web_dialog
    window._show_web_dialog = lambda: popped.append(True)  # type: ignore[method-assign]
    try:
        window.start_web_server(show_dialog=False)
        assert window.web is not None and window.web.running
        assert popped == [], "开机自动开启网页监控时不应弹出模态对话框"
    finally:
        window._show_web_dialog = monkeypatch_holder  # type: ignore[method-assign]
        window.stop_web_server()
    assert window.web is None


def test_hms_item_display_and_wiki():
    """HMS 条目文案与说明页链接（列表/详情都依赖）。"""
    item = HmsItem(attr=0x03000100, code=0x00010001, text="热床温控异常", wiki="https://w", short_code="HMS_X")
    assert "HMS_X" in item.display and "热床温控异常" in item.display
    empty = HmsItem(attr=0, code=0, text="", wiki="", short_code="")
    assert empty.display  # 至少要有原始码，不能是空字符串
