"""主窗口：监控墙（多画面网格 + 自动搜索 + 每路状态条）。"""

from __future__ import annotations

import math
import os
from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QScrollArea,
    QToolBar,
    QToolButton,
    QWidget,
)

from ..bambu.models import PrinterInfo
from ..bambu.printer import PrinterSession
from ..config import AppConfig
from . import theme
from .add_dialog import PrinterEditDialog
from .discover_dialog import DiscoverDialog
from .tile import CameraTile

REFRESH_MS = 100
#: 画面最小宽度（低于这个值就没法看了）；用于计算「窗口宽度最多能放几列」
MIN_TILE_WIDTH = 210
#: 网格间距
GRID_SPACING = 8
LAYOUT_OPTIONS = [
    ("自适应", 0),
    ("1 列", 1),
    ("2 列", 2),
    ("3 列", 3),
    ("4 列", 4),
]
PATROL_INTERVAL_MS = 10000


class MainWindow(QMainWindow):
    """拓竹打印机监控墙。"""

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.setWindowTitle("拓竹打印机监控台 · Bambu Monitor")
        self.resize(1280, 800)
        self.setMinimumSize(720, 480)

        self.sessions: list[PrinterSession] = []
        self.tiles: list[CameraTile] = []
        self.single_tile: Optional[CameraTile] = None
        self.web = None

        self._build_toolbar()
        self._build_center()
        self._build_statusbar()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(max(50, int(config.refresh_ms or 150)))
        self._refresh_timer.timeout.connect(self._refresh_tiles)
        self._refresh_timer.start()

        self._summary_timer = QTimer(self)
        self._summary_timer.setInterval(1000)
        self._summary_timer.timeout.connect(self._update_summary)
        self._summary_timer.start()

        self._patrol_timer = QTimer(self)
        self._patrol_timer.setInterval(PATROL_INTERVAL_MS)
        self._patrol_timer.timeout.connect(self._patrol_next)

        for info in config.printers:
            self.add_printer(info, autostart=config.auto_connect)
        if not self.tiles:
            QTimer.singleShot(600, self.auto_discover)
        if config.web_enabled:
            QTimer.singleShot(1500, lambda: self.start_web_server(show_dialog=False))
        if config.last_error:
            # 读取配置时已有降级（例如访问代码解不开），启动后立刻让用户看到
            QTimer.singleShot(300, self.warn_config_problem)

    # ------------------------------------------------------------------ 界面搭建
    def _build_toolbar(self) -> None:
        bar = QToolBar("主工具栏", self)
        bar.setMovable(False)
        bar.setIconSize(QSize(16, 16))
        self.addToolBar(bar)

        self.action_discover = QAction("🔍 自动搜索", self)
        self.action_discover.setShortcut(QKeySequence("F5"))
        self.action_discover.triggered.connect(self.auto_discover)
        bar.addAction(self.action_discover)

        self.action_add = QAction("＋ 添加打印机", self)
        self.action_add.setShortcut(QKeySequence("Ctrl+N"))
        self.action_add.triggered.connect(self.add_printer_dialog)
        bar.addAction(self.action_add)

        bar.addSeparator()

        self.action_connect = QAction("▶ 全部连接", self)
        self.action_connect.triggered.connect(self.connect_all)
        bar.addAction(self.action_connect)

        self.action_disconnect = QAction("■ 全部断开", self)
        self.action_disconnect.triggered.connect(self.disconnect_all)
        bar.addAction(self.action_disconnect)

        bar.addSeparator()
        label = QLabel(" 布局 ")
        label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        bar.addWidget(label)
        self.layout_combo = QComboBox()
        for text, value in LAYOUT_OPTIONS:
            self.layout_combo.addItem(text, value)
        index = self.layout_combo.findData(self.config.columns)
        self.layout_combo.setCurrentIndex(max(0, index))
        self.layout_combo.currentIndexChanged.connect(self._on_layout_changed)
        bar.addWidget(self.layout_combo)

        bar.addSeparator()
        self.action_patrol = QAction("⟳ 轮巡", self)
        self.action_patrol.setCheckable(True)
        self.action_patrol.toggled.connect(self._on_patrol_toggled)
        bar.addAction(self.action_patrol)

        self.action_snapshot = QAction("📷 全部抓拍", self)
        self.action_snapshot.triggered.connect(self.snapshot_all)
        bar.addAction(self.action_snapshot)

        bar.addSeparator()
        self.action_layout = QAction("▦ 画面布局", self)
        self.action_layout.setToolTip("调整画面顺序与大小（例如让 X2D 占大格）")
        self.action_layout.triggered.connect(self.open_layout_dialog)
        bar.addAction(self.action_layout)

        self.action_web = QAction("🌐 网页监控", self)
        self.action_web.setCheckable(True)
        self.action_web.setToolTip("开启内置网页服务，手机/其它电脑用浏览器远程观看")
        self.action_web.toggled.connect(self.toggle_web_server)
        bar.addAction(self.action_web)

        self.action_fullscreen = QAction("⛶ 全屏", self)
        self.action_fullscreen.setShortcut(QKeySequence("F11"))
        self.action_fullscreen.setCheckable(True)
        self.action_fullscreen.triggered.connect(self.toggle_fullscreen)
        bar.addAction(self.action_fullscreen)

        self.action_about = QAction("? 关于", self)
        self.action_about.triggered.connect(self.show_about)
        bar.addAction(self.action_about)

        self.action_settings = QAction("⚙ 设置", self)
        self.action_settings.setToolTip("帧率 / 界面刷新 / 网页监控参数")
        self.action_settings.triggered.connect(self.open_settings_dialog)
        bar.addAction(self.action_settings)

        bar.addSeparator()
        self.action_save = QAction("💾 保存配置", self)
        self.action_save.setToolTip("立即把打印机列表与设置写入配置文件（自动保留上一份备份）")
        self.action_save.triggered.connect(self.save_config_now)
        self.action_export = QAction("导出配置…", self)
        self.action_export.triggered.connect(self.export_config)
        self.action_import = QAction("导入配置…", self)
        self.action_import.triggered.connect(self.import_config)

        save_button = QToolButton(self)
        save_button.setDefaultAction(self.action_save)
        save_button.setPopupMode(QToolButton.MenuButtonPopup)
        save_menu = QMenu(save_button)
        save_menu.addAction(self.action_export)
        save_menu.addAction(self.action_import)
        save_button.setMenu(save_menu)
        bar.addWidget(save_button)

    def _build_center(self) -> None:
        from PySide6.QtWidgets import QStackedWidget

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        # 监控墙只在纵向滚动（画面数量多时向下滚动）；
        # 横向永远适配窗口宽度，避免出现「画面跑到屏幕外」。
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.stack = QStackedWidget()
        self.empty_label = QLabel(
            "还没有添加打印机。\n\n"
            "点击工具栏「🔍 自动搜索」扫描局域网，"
            "或点「＋ 添加打印机」手动输入 IP 与访问代码。"
        )
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: 14px; border: 1px dashed {theme.BORDER};"
            f" border-radius: 8px; padding: 40px;"
        )

        self.grid_page = QWidget()
        self.grid = QGridLayout(self.grid_page)
        self.grid.setContentsMargins(8, 8, 8, 8)
        self.grid.setSpacing(8)

        self.stack.addWidget(self.empty_label)
        self.stack.addWidget(self.grid_page)
        self.scroll.setWidget(self.stack)
        self.setCentralWidget(self.scroll)
        self.stack.setCurrentWidget(self.empty_label)

    def _build_statusbar(self) -> None:
        self.status_summary = QLabel("准备就绪")
        self.statusBar().addWidget(self.status_summary)
        self.status_hint = QLabel("")
        self.status_hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.statusBar().addPermanentWidget(self.status_hint)

    # ------------------------------------------------------------------ 打印机管理
    def _persist(self) -> None:
        """立即写盘：避免程序异常退出时丢失刚填好的访问代码。"""
        self.config.printers = [session.info for session in self.sessions]
        self.config.save()

    def add_printer(self, info: PrinterInfo, autostart: bool = True) -> None:
        for existing in self.sessions:
            same_serial = info.serial and existing.info.serial == info.serial
            if same_serial or (not info.serial and existing.info.ip == info.ip):
                # 更新已有条目（例如补上访问代码）
                existing.info.access_code = info.access_code or existing.info.access_code
                existing.info.name = info.name or existing.info.name
                existing.info.model = (
                    info.model if info.model.value != "未知机型" else existing.info.model
                )
                if autostart and not existing.running:
                    existing.start()
                self._persist()
                return

        session = PrinterSession(info)
        self.sessions.append(session)
        tile = CameraTile(session, self)
        tile.request_single_view.connect(self.toggle_single_view)
        tile.request_edit.connect(self.edit_printer)
        tile.request_remove.connect(self.remove_tile)
        tile.request_reconnect.connect(self.reconnect_tile)
        tile.request_diagnose.connect(self.diagnose_tile)
        tile.request_layout.connect(self.handle_layout_action)
        tile.notify.connect(self._notify)
        self.tiles.append(tile)

        if info not in self.config.printers:
            self.config.printers.append(info)

        if autostart:
            session.start()
        session.set_max_fps(self.config.max_fps)

        self.empty_label.setVisible(False)
        self.rebuild_grid()
        self._persist()

    def remove_tile(self, tile: CameraTile) -> None:
        session = tile.session
        answer = QMessageBox.question(
            self,
            "移除打印机",
            f"确定要从监控墙移除「{session.info.display_name()}」吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        session.stop()
        tile.shutdown()
        if session.info in self.config.printers:
            self.config.printers.remove(session.info)
        if self.single_tile is tile:
            self.single_tile = None
        self.sessions.remove(session)
        self.tiles.remove(tile)
        tile.setParent(None)
        tile.deleteLater()
        self.empty_label.setVisible(not self.tiles)
        self.rebuild_grid()
        self._persist()

    def edit_printer(self, tile: CameraTile) -> None:
        dialog = PrinterEditDialog(self, tile.session.info)
        if dialog.exec() != PrinterEditDialog.Accepted or dialog.result is None:
            return
        info = dialog.result
        old = tile.session.info
        index = self.config.printers.index(old) if old in self.config.printers else -1
        old.ip, old.name, old.serial = info.ip, info.name, info.serial
        old.access_code, old.model, old.stream_mode = (
            info.access_code,
            info.model,
            info.stream_mode,
        )
        if index >= 0:
            self.config.printers[index] = old
        tile.session.restart()
        self._persist()
        self._notify("已更新设置并重新连接")

    def reconnect_tile(self, tile: CameraTile) -> None:
        tile.video.clear_frame()
        tile.session.restart()
        self._notify(f"正在重新连接 {tile.session.info.display_name()}…")

    def diagnose_tile(self, tile: CameraTile) -> None:
        from .diagnose_dialog import DiagnoseDialog

        dialog = DiagnoseDialog(tile.session.info, self)
        dialog.exec()
        self._persist()

    # ------------------------------------------------------------------ 画面布局
    def open_layout_dialog(self) -> None:
        if not self.tiles:
            self._notify("还没有画面可以调整")
            return
        from .layout_dialog import LayoutDialog

        dialog = LayoutDialog(self.tiles, self)
        if dialog.exec() != LayoutDialog.Accepted:
            return
        self.tiles = dialog.ordered_tiles
        self.rebuild_grid()
        self._persist()
        self._notify("已更新画面顺序与大小")

    def handle_layout_action(self, tile: CameraTile, action: str) -> None:
        info = tile.session.info
        if action == "dialog":
            self.open_layout_dialog()
            return
        if action == "focus_on":
            info.tile_span = 2
        elif action == "focus_off":
            info.tile_span = 1
        elif action in ("up", "down", "top", "bottom"):
            if tile not in self.tiles:
                return
            index = self.tiles.index(tile)
            if action == "up" and index > 0:
                self.tiles[index - 1], self.tiles[index] = self.tiles[index], self.tiles[index - 1]
            elif action == "down" and index < len(self.tiles) - 1:
                self.tiles[index + 1], self.tiles[index] = self.tiles[index], self.tiles[index + 1]
            elif action == "top":
                self.tiles.insert(0, self.tiles.pop(index))
            elif action == "bottom":
                self.tiles.append(self.tiles.pop(index))
        self.rebuild_grid()
        self._persist()

    def add_printer_dialog(self) -> None:
        dialog = PrinterEditDialog(self)
        if dialog.exec() == PrinterEditDialog.Accepted and dialog.result is not None:
            self.add_printer(dialog.result)
            self._notify(f"已添加 {dialog.result.display_name()}")

    def connect_all(self) -> None:
        started = 0
        skipped = 0
        for session in self.sessions:
            if not session.info.access_code:
                skipped += 1
                continue
            if not session.running:
                session.start()
                started += 1
        message = f"已启动 {started} 台打印机的连接"
        if skipped:
            message += f"，{skipped} 台因缺少访问代码被跳过"
        self._notify(message)

    def disconnect_all(self) -> None:
        for session in self.sessions:
            session.stop()
        self._notify("已断开全部打印机")

    # ------------------------------------------------------------------ 自动搜索
    def auto_discover(self) -> None:
        """打开自动搜索对话框（对话框内部负责扫描与展示结果）。"""
        dialog = DiscoverDialog(
            known=self.config.printers, timeout=self.config.last_timeout, parent=self
        )
        dialog.exec()
        selected = dialog.selected
        if not selected:
            if dialog.table.rowCount() == 0:
                self._notify("未发现打印机，请检查网络或手动添加")
            return
        for info in selected:
            self.add_printer(info)
        self._notify(f"已加入 {len(selected)} 台打印机")

    # ------------------------------------------------------------------ 布局
    def rebuild_grid(self) -> None:
        self.stack.setCurrentWidget(self.grid_page if self.tiles else self.empty_label)
        self.empty_label.setVisible(not self.tiles)

        for tile in self.tiles:
            self.grid.removeWidget(tile)
            tile.setVisible(False)

        visible = [tile for tile in self.tiles if self.single_tile in (None, tile)]
        if not visible:
            return
        max_span = max((self._span_of(tile) for tile in visible), default=1)
        columns = max(self._effective_columns(len(visible)), max_span)
        for column in range(12):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)

        # 按顺序装箱：重点画面占 2×2（或 3×3），其余画面自动填满空位
        occupied: set[tuple[int, int]] = set()
        max_row = 0
        for tile in visible:
            span = self._span_of(tile)
            slot = self._find_slot(occupied, columns, span)
            if slot is None:
                continue
            row, column = slot
            for dr in range(span):
                for dc in range(span):
                    occupied.add((row + dr, column + dc))
            self.grid.addWidget(tile, row, column, span, span)
            tile.setVisible(True)
            max_row = max(max_row, row + span)
        for row in range(12):
            self.grid.setRowStretch(row, 1 if row < max_row else 0)

    @staticmethod
    def _span_of(tile: CameraTile) -> int:
        return max(1, min(3, int(tile.session.info.tile_span or 1)))

    @staticmethod
    def _find_slot(
        occupied: set[tuple[int, int]], columns: int, span: int, max_rows: int = 24
    ) -> Optional[tuple[int, int]]:
        """在占用表里找第一块能放下 span×span 的空位。"""
        for row in range(max_rows):
            for column in range(0, max(1, columns - span + 1)):
                cells = [(row + dr, column + dc) for dr in range(span) for dc in range(span)]
                if all(cell not in occupied for cell in cells):
                    return row, column
        return None

    def _effective_columns(self, count: int) -> int:
        if count <= 1:
            return 1
        if self.config.columns > 0:
            return min(self.config.columns, count)
        viewport = self.scroll.viewport().size()
        # 先按窗口宽度算「最多能放几列」：否则网格会被撑得比窗口宽，
        # 画面就跑到屏幕外、要手动横向滚动才能看见。
        usable = max(120, viewport.width() - GRID_SPACING * 2)
        max_columns = max(1, int(usable // (MIN_TILE_WIDTH + GRID_SPACING)))
        best_columns = 1
        best_score = -1.0
        for columns in range(1, count + 1):
            rows = math.ceil(count / columns)
            tile_width = (viewport.width() - GRID_SPACING * (columns + 1)) / columns
            tile_height = (viewport.height() - GRID_SPACING * (rows + 1)) / rows - 132
            if tile_width < 80 or tile_height < 45:
                continue
            score = min(tile_width / 1280.0, tile_height / 720.0)
            if score > best_score:
                best_score = score
                best_columns = columns
        return max(1, min(best_columns, max_columns, count))

    def _on_layout_changed(self) -> None:
        self.config.columns = int(self.layout_combo.currentData())
        self.rebuild_grid()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.config.columns == 0 and self.tiles:
            if not hasattr(self, "_layout_timer"):
                self._layout_timer = QTimer(self)
                self._layout_timer.setSingleShot(True)
                self._layout_timer.setInterval(150)
                self._layout_timer.timeout.connect(self.rebuild_grid)
            self._layout_timer.start()

    def toggle_single_view(self, tile: CameraTile) -> None:
        self.single_tile = None if self.single_tile is tile else tile
        self.rebuild_grid()

    def _patrol_next(self) -> None:
        if not self.tiles:
            return
        if self.single_tile in self.tiles:
            index = (self.tiles.index(self.single_tile) + 1) % len(self.tiles)
        else:
            index = 0
        self.single_tile = self.tiles[index]
        self.rebuild_grid()

    def _on_patrol_toggled(self, enabled: bool) -> None:
        if enabled:
            self._patrol_timer.start()
            self._patrol_next()
            self._notify("已开启轮巡：每 10 秒切换一路画面")
        else:
            self._patrol_timer.stop()
            self.single_tile = None
            self.rebuild_grid()
            self._notify("已关闭轮巡")

    def snapshot_all(self) -> None:
        saved = 0
        for tile in self.tiles:
            if tile.video.has_frame():
                tile.save_snapshot()
                saved += 1
        self._notify(f"已抓拍 {saved} 路画面" if saved else "当前没有可抓拍的画面")

    # ------------------------------------------------------------------ 刷新
    def _refresh_tiles(self) -> None:
        for tile in self.tiles:
            if tile.isVisible():
                tile.refresh()

    def _update_summary(self) -> None:
        if not self.sessions:
            self.status_summary.setText("还没有打印机 —— 点击「自动搜索」开始")
            return
        total = len(self.sessions)
        online = sum(1 for session in self.sessions if session.status.mqtt_online)
        camera = sum(1 for session in self.sessions if session.status.camera_online)
        printing = sum(1 for session in self.sessions if session.snapshot().is_printing)
        self.status_summary.setText(
            f"共 {total} 台 · 遥测在线 {online} · 画面在线 {camera} · 正在打印 {printing}"
        )

    def _notify(self, text: str) -> None:
        self.statusBar().showMessage(text, 6000)

    # ------------------------------------------------------------------ 配置
    def save_config_now(self) -> None:
        self._persist()
        from ..config import config_path

        if self.config.last_error:
            # 以前写盘失败是静默的：用户看到「已保存」，其实配置根本没落盘
            QMessageBox.warning(
                self,
                "配置未能完整保存",
                f"{self.config.last_error}\n\n目标文件：{config_path()}",
            )
            self._notify(f"⚠ {self.config.last_error}")
            return
        self._notify(f"配置已保存（{len(self.sessions)} 台）→ {config_path()}")

    def warn_config_problem(self) -> None:
        """启动时提示配置读取阶段的降级（例如访问代码解不开）。"""
        if not self.config.last_error:
            return
        self._notify(f"⚠ {self.config.last_error}")

    def export_config(self) -> None:
        from PySide6.QtWidgets import QFileDialog, QStandardPaths

        # 与抓拍一致，默认落到「文档」而不是用户主目录，避免用户找不到导出文件
        base = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation) or os.path.expanduser("~")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出配置",
            os.path.join(base, "bambu-monitor-config.json"),
            "JSON 文件 (*.json)",
        )
        if not path:
            return
        if self.config.export_to(path):
            if self.config.last_error:
                QMessageBox.warning(self, "导出提示", self.config.last_error)
            self._notify(f"配置已导出：{path}")
        else:
            QMessageBox.warning(
                self, "导出失败", self.config.last_error or "无法写入该文件，请换一个位置再试。"
            )

    def import_config(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(self, "导入配置", "", "JSON 文件 (*.json)")
        if not path:
            return
        if not self.config.import_from(path):
            QMessageBox.warning(
                self,
                "导入失败",
                "文件无法解析，或里面没有打印机。\n"
                "注意：访问代码使用当前 Windows 用户加密，换用户/换电脑后需要重新填写。",
            )
            return
        for session in self.sessions:
            session.stop()
        for tile in self.tiles:
            tile.shutdown()
            tile.setParent(None)
            tile.deleteLater()
        self.sessions.clear()
        self.tiles.clear()
        self.single_tile = None
        for info in self.config.printers:
            self.add_printer(info, autostart=self.config.auto_connect)
        self.rebuild_grid()
        self._persist()
        self._notify(f"已导入 {len(self.tiles)} 台打印机")

    # ------------------------------------------------------------------ 网页监控
    def open_settings_dialog(self) -> None:
        from .settings_dialog import SettingsDialog

        before_port = self.config.web_port
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() != SettingsDialog.Accepted:
            return
        for session in self.sessions:
            session.set_max_fps(self.config.max_fps)
        self._refresh_timer.setInterval(max(50, int(self.config.refresh_ms)))
        if self.web is not None and self.web.running and self.config.web_port != before_port:
            self.stop_web_server()
            self.start_web_server(show_dialog=False)
            self._notify(f"网页端口已改为 {self.config.web_port}")
        self._persist()
        self._notify("设置已保存")

    def toggle_web_server(self, enabled: bool) -> None:
        if enabled:
            self.start_web_server(show_dialog=True)
        else:
            self.stop_web_server()
            self.config.web_enabled = False
            self._persist()
            self._notify("已关闭网页监控")

    def start_web_server(self, show_dialog: bool = False) -> None:
        from ..web.server import WebServer

        if self.web is not None and self.web.running:
            if show_dialog:
                self._show_web_dialog()
            return
        server = WebServer(
            get_sessions=lambda: list(self.sessions),
            port=self.config.web_port,
            token=self.config.web_token,
            fps=self.config.web_fps,
            max_width=self.config.web_max_width,
        )
        if not server.start():
            self.action_web.setChecked(False)
            QMessageBox.warning(
                self,
                "网页监控启动失败",
                f"端口 {self.config.web_port} 可能已被占用。\n"
                "请关闭占用该端口的程序，或在配置里改用其它端口。",
            )
            return
        self.web = server
        self.config.web_enabled = True
        self.action_web.setChecked(True)
        self._persist()
        self._notify(f"网页监控已开启：{server.primary_url()}")
        if show_dialog:
            self._show_web_dialog()

    def stop_web_server(self) -> None:
        if self.web is not None:
            self.web.stop()
            self.web = None
        if hasattr(self, "action_web"):
            self.action_web.setChecked(False)

    def _show_web_dialog(self) -> None:
        if self.web is None:
            return
        from .web_dialog import WebInfoDialog

        dialog = WebInfoDialog(self.web.urls(), self.web.port, self)
        dialog.exec()

    # ------------------------------------------------------------------ 其它
    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.action_fullscreen.setChecked(False)
        else:
            self.showFullScreen()
            self.action_fullscreen.setChecked(True)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            if self.isFullScreen():
                self.showNormal()
                self.action_fullscreen.setChecked(False)
            elif self.single_tile is not None:
                self.single_tile = None
                self.rebuild_grid()
            return
        super().keyPressEvent(event)

    def show_about(self) -> None:
        QMessageBox.information(
            self,
            "关于",
            "拓竹打印机监控台（Bambu Monitor）\n\n"
            "· 自动搜索：SSDP 组播（239.255.255.250:1990）+ 2021 端口广播\n"
            "· 遥测：MQTT over TLS（8883，bblp + 访问代码）\n"
            "· 画面：6000 端口 JPEG 流（TLS），X1/P2S/H2/X2D 可回退 RTSPS(322)\n"
            "· 通道均为打印机官方局域网接口，不经过云端\n\n"
            "快捷键：F5 自动搜索 · Ctrl+N 添加 · F11 全屏 · Esc 退出单画面",
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        self._refresh_timer.stop()
        self._summary_timer.stop()
        self._patrol_timer.stop()
        for tile in self.tiles:
            tile.shutdown()
        self.stop_web_server()
        for session in self.sessions:
            session.stop()
        try:
            self.config.window_geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        except Exception:
            pass
        self._persist()
        super().closeEvent(event)
