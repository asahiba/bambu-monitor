"""单路监控画面：视频 + 下方状态条（进度 / 喷嘴温度 / 热床温度）。"""

from __future__ import annotations

import os
import time
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..bambu.models import PrinterStatus
from ..bambu.printer import PrinterSession
from ..core.device import camera_status_text
from . import theme
from .frame_decoder import FrameDecoder
from .video_widget import VideoWidget


class _Badge(QLabel):
    """状态徽标（打印中/空闲/离线…）。"""

    COLORS = {
        "打印中": theme.PRINTING,
        "准备中": theme.PRINTING,
        "已暂停": theme.WARN,
        "打印完成": theme.OK,
        "打印失败": theme.ERROR,
        "空闲": theme.TEXT_DIM,
        "离线": theme.ERROR,
        "未知": theme.TEXT_DIM,
    }

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumWidth(58)
        self._text = ""

    def set_state(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        color = self.COLORS.get(text, theme.TEXT_DIM)
        self.setText(text)
        self.setStyleSheet(
            f"color: {color}; border: 1px solid {color}; border-radius: 3px;"
            f" padding: 1px 6px; font-weight: bold; background: rgba(0,0,0,60);"
        )


class _ValueLabel(QLabel):
    """带标题的数值显示，例如 喷嘴 245.3/250.0 ℃。"""

    def __init__(self, caption: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._caption = caption
        self._current = "--"
        self._target = "--"
        self._text_cache = ""
        font = QFont(theme.FONT_FAMILY, 9)
        self.setFont(font)

    def set_values(self, current: Optional[float], target: Optional[float]) -> None:
        cur = "--" if current is None else f"{current:.0f}"
        tar = "--" if target is None else f"{target:.0f}"
        if cur == self._current and tar == self._target:
            return
        self._current, self._target = cur, tar
        html = (
            f"<span style='color:{theme.TEXT_DIM}'>{self._caption}</span>&nbsp;"
            f"<span style='color:{theme.ACCENT};font-weight:bold'>{cur}</span>"
            f"<span style='color:{theme.TEXT_DIM}'>/{tar}℃</span>"
        )
        self.setText(html)

    def clear(self) -> None:
        self.set_values(None, None)


class CameraTile(QFrame):
    """一路监控画面（视频 + 状态条）。"""

    request_single_view = Signal(object)
    request_edit = Signal(object)
    request_remove = Signal(object)
    request_reconnect = Signal(object)
    request_diagnose = Signal(object)
    request_layout = Signal(object, str)
    notify = Signal(str)

    def __init__(self, session: PrinterSession, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            f"CameraTile {{ background: {theme.PANEL}; border: 1px solid {theme.BORDER};"
            f" border-radius: 6px; }}"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        self.video = VideoWidget(self)
        self.video.double_clicked.connect(lambda: self.request_single_view.emit(self))
        layout.addWidget(self.video, 1)

        layout.addWidget(self._build_status_panel())

        # 解码在后台线程完成（12 路 1080p 时这是性能关键）
        self.decoder = FrameDecoder(session, 640, 360)
        self.decoder.start()
        self._last_seq = -1
        self._last_state_text = ""
        self._text_cache: dict = {}
        self._progress_accent = ""
        self._control_block_reason = ""
        self._tooltip = ""
        self._filament_signature: object = None
        self._snapshot_dir = os.path.join(_pictures_dir(), "BambuMonitor")

    def shutdown(self) -> None:
        """从监控墙移除时停止后台解码线程。

        必须等一下线程真正退出：解码线程会在之后继续碰 ``QImage``，
        若它还在跑而 Qt 已经开始拆对象，进程会直接 fail-fast 消失
        （和诊断对话框那个崩溃是同一类问题，见 `app/ui/qt_threads.py`）。
        """
        self.decoder.stop()
        self.decoder.join(timeout=1.0)

    # ------------------------------------------------------------------ 状态条
    def _build_status_panel(self) -> QWidget:
        panel = QWidget(self)
        panel.setStyleSheet(
            f"background: {theme.PANEL_ALT}; border-bottom-left-radius: 5px;"
            f" border-bottom-right-radius: 5px;"
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(9, 6, 9, 7)
        layout.setSpacing(4)

        # 第一行：状态 / 进度百分比 / 剩余时间
        top = QHBoxLayout()
        top.setSpacing(8)
        self.badge = _Badge()
        top.addWidget(self.badge)

        self.progress_label = QLabel("--%")
        progress_font = QFont(theme.FONT_FAMILY, 11)
        progress_font.setBold(True)
        self.progress_label.setFont(progress_font)
        self.progress_label.setStyleSheet(f"color: {theme.ACCENT};")
        top.addWidget(self.progress_label)

        self.task_label = QLabel("")
        self.task_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.task_label.setTextInteractionFlags(Qt.NoTextInteraction)
        top.addWidget(self.task_label, 1)

        self.hms_badge = QLabel("")
        self.hms_badge.setStyleSheet(
            f"color: {theme.ERROR}; border: 1px solid {theme.ERROR}; border-radius: 3px;"
            f" padding: 0 5px; font-weight: bold;"
        )
        self.hms_badge.setCursor(Qt.PointingHandCursor)
        self.hms_badge.setToolTip("点击查看 HMS 提示详情")
        self.hms_badge.mousePressEvent = self._open_hms  # type: ignore[assignment]
        self.hms_badge.setVisible(False)
        top.addWidget(self.hms_badge)

        self.remaining_label = QLabel("剩余 --")
        self.remaining_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        top.addWidget(self.remaining_label, 0, Qt.AlignRight)
        layout.addLayout(top)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(10)
        layout.addWidget(self.progress_bar)

        # 第二行：喷嘴/热床/仓温/层数/WiFi/预计完成
        # 用**单个可换行的富文本标签**：一排控件的最小宽度是各项文本宽度之和，
        # 13 路时会把整个网格撑到窗口外（「画面跑到屏幕外」的根因）。
        self.info_label = QLabel("")
        self.info_label.setWordWrap(True)
        self.info_label.setTextFormat(Qt.RichText)
        self.info_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.info_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.info_label)

        # 第三行：耗材（AMS 各槽位 + 外挂料盘），同样是单个可换行标签
        self.filament_label = QLabel("")
        self.filament_label.setWordWrap(True)
        self.filament_label.setTextFormat(Qt.RichText)
        self.filament_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.filament_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.filament_label.setVisible(False)
        layout.addWidget(self.filament_label)

        # 第四行：控制按钮（窄画面只显示图标，宽画面自动带文字）
        controls = QHBoxLayout()
        controls.setSpacing(4)
        controls.addStretch(1)
        self.pause_button = QPushButton("⏸")
        self.pause_button.setToolTip("暂停打印（可恢复）")
        self.pause_button.clicked.connect(self._toggle_pause)
        self.stop_button = QPushButton("⏹")
        self.stop_button.setToolTip("停止打印（不可恢复，会二次确认）")
        self.stop_button.clicked.connect(self._stop_print)
        self.light_button = QPushButton("💡")
        self.light_button.setToolTip("开/关舱灯")
        self.light_button.clicked.connect(self._toggle_light)
        for button in (self.pause_button, self.stop_button, self.light_button):
            button.setStyleSheet(
                f"QPushButton {{ background: {theme.PANEL}; border: 1px solid {theme.BORDER};"
                f" border-radius: 3px; padding: 1px 8px; font-size: 11px; }}"
                f"QPushButton:hover {{ border-color: {theme.ACCENT}; }}"
                f"QPushButton:disabled {{ color: #5a6a72; }}"
            )
            button.setCursor(Qt.PointingHandCursor)
            button.setMinimumWidth(34)
            controls.addWidget(button)
        layout.addLayout(controls)
        return panel

    # ------------------------------------------------------------------ 控制
    def _open_hms(self, event=None) -> None:  # noqa: ANN001
        status = self.session.snapshot()
        if not status.hms_items:
            return
        from .hms_dialog import HmsDialog

        HmsDialog(status.hms_items, self.session.info.display_name(), self).exec()

    def _toggle_pause(self) -> None:
        status = self.session.snapshot()
        name = self.session.info.display_name()
        if status.is_paused:
            ok = self.session.resume_print()
            self.notify.emit(f"{name}：{'已发送继续指令' if ok else '发送失败（遥测未连接）'}")
        else:
            ok = self.session.pause_print()
            self.notify.emit(f"{name}：{'已发送暂停指令' if ok else '发送失败（遥测未连接）'}")

    def _stop_print(self) -> None:
        name = self.session.info.display_name()
        answer = QMessageBox.question(
            self,
            "停止打印",
            f"确定要停止「{name}」当前的打印任务吗？\n\n停止后打印无法继续，需要重新开始打印。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        ok = self.session.stop_print()
        self.notify.emit(f"{name}：{'已发送停止指令' if ok else '发送失败（遥测未连接）'}")

    def _toggle_light(self) -> None:
        status = self.session.snapshot()
        name = self.session.info.display_name()
        target = not bool(status.light_on)
        ok = self.session.set_light(target)
        action = "开灯" if target else "关灯"
        self.notify.emit(f"{name}：{'已发送' + action + '指令' if ok else '发送失败（遥测未连接）'}")

    # ------------------------------------------------------------------ 刷新
    def refresh(self) -> None:
        seq, image = self.decoder.latest()
        if image is not None and seq != self._last_seq:
            self._last_seq = seq
            self.video.set_image(image)
            self.video.set_fps(self.session.camera_fps)

        info = self.session.info
        status = self.session.snapshot()
        self.video.set_title(f"{info.display_name()}", f"{info.ip} · {info.model.label}")

        state_text, color = self._connection_text(status)
        self.video.set_status(state_text, color)
        self._update_status_panel(status)

    def _connection_text(self, status: PrinterStatus) -> tuple[str, str]:
        """角标文案：与网页版共用 `app.core.camera_status_text` 的判据。

        完整说明（例如「RTSPS(322) 未取到画面…」）不再截断后塞进角标，而是
        走 `setToolTip`（见 `_update_status_panel`），角标只放稳定的短标签。
        """
        label, _detail = camera_status_text(
            camera_online=status.camera_online,
            mqtt_online=status.mqtt_online,
            camera_state=self.session.last_camera_state,
            camera_detail=self.session.last_camera_detail,
            mqtt_auth_error=self.session.mqtt_auth_error,
            has_access_code=bool(self.session.info.access_code),
        )
        color = {
            "在线": theme.OK,
            "画面正常·遥测断开": theme.WARN,
            "访问代码错误": theme.ERROR,
            "未配置访问代码": theme.ERROR,
        }.get(label, theme.WARN)
        return label, color

    def _set_text(self, widget: QLabel, value: str) -> None:
        """只在文本变化时 setText：避免每 150ms 触发一次布局重算。"""
        if self._text_cache.get(widget) != value:
            self._text_cache[widget] = value
            widget.setText(value)

    def _update_status_panel(self, status: PrinterStatus) -> None:
        state = status.state_text if (status.mqtt_online or status.gcode_state) else "离线"
        if state != self._last_state_text:
            self._last_state_text = state
            self.badge.set_state(state)

        percent = max(0, min(100, status.progress))
        self._set_text(self.progress_label, f"{percent}%")
        if self.progress_bar.value() != percent:
            self.progress_bar.setValue(percent)

        accent = theme.PRINTING if status.is_printing else theme.TEXT_DIM
        if accent != self._progress_accent:
            self._progress_accent = accent
            self.progress_bar.setStyleSheet(
                f"QProgressBar {{ background: #101a1f; border: 1px solid {theme.BORDER};"
                f" border-radius: 5px; }}"
                f"QProgressBar::chunk {{ background: {accent}; border-radius: 4px; }}"
            )

        if status.mqtt_online or status.last_message_ts:
            self._set_text(self.remaining_label, f"剩余 {status.remaining_text}")
        else:
            self._set_text(self.remaining_label, "剩余 --")

        task = status.subtask_name or ""
        if task:
            task = os.path.splitext(os.path.basename(task))[0]
        self._set_text(self.task_label, task)

        self._update_info_row(status)
        self._update_filament_row(status)
        self._update_controls(status)

        if status.hms_items:
            self.hms_badge.setText(f"⚠ HMS×{len(status.hms_items)}")
            self.hms_badge.setVisible(True)
        else:
            self.hms_badge.setVisible(False)

        # 把通道提示（例如 RTSPS 回退、缺少依赖）放到鼠标提示里
        tips = list(self.session.warnings)
        if status.last_error:
            tips.append(status.last_error)
        if self.session.last_camera_detail:
            tips.append(f"视频通道：{self.session.video_backend}（{self.session.last_camera_detail}）")
        tooltip = "\n".join(dict.fromkeys(tips)) if tips else ""
        if tooltip != self._tooltip:
            self._tooltip = tooltip
            self.setToolTip(tooltip)

    def _update_info_row(self, status: PrinterStatus) -> None:
        """喷嘴/热床/仓温/层数/WiFi/预计完成/错误码，渲染成一个可换行的富文本标签。"""
        if not (status.mqtt_online or status.last_message_ts):
            self._set_text(self.info_label, "")
            return
        accent = theme.ACCENT
        dim = theme.TEXT_DIM
        parts = [
            f"喷嘴 <b style='color:{accent}'>{status.nozzle_temper:.0f}</b>"
            f"<span style='color:{dim}'>/{status.nozzle_target_temper:.0f}℃</span>",
            f"热床 <b style='color:{accent}'>{status.bed_temper:.0f}</b>"
            f"<span style='color:{dim}'>/{status.bed_target_temper:.0f}℃</span>",
        ]
        if status.chamber_temper is not None and self.session.capabilities.has_chamber_sensor:
            parts.append(f"仓温 {status.chamber_temper:.0f}℃")
        if status.total_layer_num > 0:
            parts.append(f"层 {status.layer_num}/{status.total_layer_num}")
        if status.wifi_signal:
            bars = status.wifi_level
            parts.append(f"WiFi {'▮' * bars}{'▯' * (4 - bars)} {status.wifi_signal}")
        if status.finish_time_text != "--":
            parts.append(f"预计完成 {status.finish_time_text}")
        if status.print_error:
            label = f"E{status.print_error}"
            if status.print_error_text:
                label = f"{label} {status.print_error_text}"
            parts.append(f"<b style='color:{theme.ERROR}'>{label}</b>")
        self._set_text(self.info_label, "&nbsp;&nbsp;".join(parts))

    def _update_filament_row(self, status: PrinterStatus) -> None:
        """耗材行：AMS 各槽位色块 + 类型 + 余量，以及外挂料盘（单个可换行标签）。"""
        entries = list(status.ams_trays)
        if status.vt_tray is not None and not status.vt_tray.empty:
            entries.append(status.vt_tray)
        if not entries:
            if self.filament_label.isVisible():
                self.filament_label.setVisible(False)
            self._set_text(self.filament_label, "")
            return

        active = status.active_tray
        chips = []
        for tray in entries:
            color = tray.color_hex or "#3a3a3a"
            is_active = active is not None and tray is active
            # 余量读不到时（非官方料卷 / 外挂）就不显示，避免误导
            text = f"{tray.label} {tray.tray_type or '--'}"
            remain = tray.remain_text
            if remain:
                text = f"{text} {remain}"
            square = f"<span style='color:{color}'>■</span>"
            if is_active:
                chips.append(f"<b style='color:{theme.ACCENT}'>{square} {text}</b>")
            else:
                chips.append(f"{square} {text}")
        self._set_text(self.filament_label, "耗材&nbsp;&nbsp;" + "&nbsp;&nbsp;".join(chips))
        if not self.filament_label.isVisible():
            self.filament_label.setVisible(True)
        self.filament_label.setToolTip(
            "\n".join(
                f"{tray.label}：{tray.tray_type or '未知'} {tray.color_hex or ''}，{tray.remain_hint}"
                f"（喷嘴 {tray.nozzle_temp_min}-{tray.nozzle_temp_max}℃）"
                for tray in entries
            )
        )

    def _apply_button_labels(self, paused: bool) -> None:
        """宽画面显示文字，窄画面只显示图标（避免把画面挤出屏幕）。"""
        wide = self.width() >= 380
        if wide:
            self.pause_button.setText("▶ 继续" if paused else "⏸ 暂停")
            self.stop_button.setText("⏹ 停止")
            self.light_button.setText("💡 关灯" if self.session.snapshot().light_on else "💡 开灯")
        else:
            self.pause_button.setText("▶" if paused else "⏸")
            self.stop_button.setText("⏹")
            self.light_button.setText("💡")

    def _update_controls(self, status: PrinterStatus) -> None:
        """控制按钮的可用状态与文案。

        ⚠️ 按钮可用性要**按命令分别判断**：固件要求 MQTT 命令签名时，
        只有 `print` 段命令（暂停/停止/速度）会被忽略，而灯控走 `system` 段、
        不受影响 —— 实测确认 A2L 在需签名的状态下灯控仍然生效。
        以前用一刀切的 `can_control` 会把灯按钮也置灰，等于把能用的功能锁住。
        """
        online = self.session.can_control
        printing = status.is_printing
        paused = status.is_paused
        gcode = (status.gcode_state or "").upper()
        caps = self.session.capabilities

        def blocked(command: str) -> str:
            checker = getattr(self.session, "command_blocked", None)
            return checker(command) if callable(checker) else ""

        pause_block, stop_block, light_block = blocked("pause"), blocked("stop"), blocked("light")

        self.pause_button.setVisible(printing)
        self.pause_button.setEnabled(online and printing and not pause_block)
        self.stop_button.setVisible(printing and gcode != "FINISH")
        self.stop_button.setEnabled(online and printing and not stop_block)
        # 灯按钮按**设备是否真的上报了灯**决定显隐，而不是按机型猜。
        # 教训：A2L 是开放式机型，我据此推断它没有舱灯，实测却上报了
        # chamber_light —— 结果界面把按钮藏了，用户没法开关灯。
        # 遥测还没上来时（light_on 为 None）才退回机型能力作为兜底。
        has_light = status.light_on is not None or caps.can_control_light
        self.light_button.setVisible(has_light)
        self.light_button.setEnabled(has_light and online and not light_block)
        self._apply_control_block_hint(pause_block or stop_block, light_block)
        self._apply_button_labels(paused)

    def _apply_control_block_hint(self, print_reason: str, light_reason: str) -> None:
        """控制被「固件要求命令签名」挡住时，把原因写到对应按钮的提示上。

        实测：新机型（H2C / H2S / X2D / P2S / A2L）的 `fun` 字段会置位命令签名要求，
        未开开发者模式时 `print` 段命令（暂停/停止/速度）会被固件静默忽略；
        **灯控不受影响**。按钮置灰却不说明原因会让人以为软件坏了，所以把原因挂上。
        """
        reason = print_reason or light_reason
        if reason == self._control_block_reason:
            return
        self._control_block_reason = reason
        self.pause_button.setToolTip(print_reason or "")
        self.stop_button.setToolTip(print_reason or "")
        self.light_button.setToolTip(light_reason or "开/关舱灯")
        # 置灰时给个视觉提示，避免用户反复点击
        for button, blocked_reason in (
            (self.pause_button, print_reason),
            (self.stop_button, print_reason),
            (self.light_button, light_reason),
        ):
            button.setStyleSheet("color: #8fa3ad;" if blocked_reason else "")

    # ------------------------------------------------------------------ 交互
    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        menu.addAction("单画面 / 退出单画面", lambda: self.request_single_view.emit(self))
        menu.addAction("重新连接", lambda: self.request_reconnect.emit(self))
        menu.addAction("画面通道诊断…", lambda: self.request_diagnose.emit(self))
        menu.addAction("抓拍保存图片", self.save_snapshot)
        menu.addSeparator()

        layout_menu = menu.addMenu("画面顺序与大小")
        focused = self.session.info.tile_span >= 2
        layout_menu.addAction(
            "取消重点画面" if focused else "设为重点画面（占 2×2）",
            lambda: self.request_layout.emit(self, "focus_off" if focused else "focus_on"),
        )
        layout_menu.addSeparator()
        layout_menu.addAction("上移", lambda: self.request_layout.emit(self, "up"))
        layout_menu.addAction("下移", lambda: self.request_layout.emit(self, "down"))
        layout_menu.addAction("移到最前", lambda: self.request_layout.emit(self, "top"))
        layout_menu.addAction("移到最后", lambda: self.request_layout.emit(self, "bottom"))
        layout_menu.addSeparator()
        layout_menu.addAction("打开布局窗口…", lambda: self.request_layout.emit(self, "dialog"))

        menu.addSeparator()
        menu.addAction("编辑打印机…", lambda: self.request_edit.emit(self))
        copy_action = QAction("复制 IP 地址", menu)
        copy_action.triggered.connect(lambda: self._copy_ip())
        menu.addAction(copy_action)
        menu.addSeparator()
        menu.addAction("从监控墙移除", lambda: self.request_remove.emit(self))
        menu.exec(event.globalPos())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        size = self.video.size()
        if size.width() > 16 and size.height() > 16:
            self.decoder.set_target_size(size.width(), size.height())
            self.session.set_video_target_size(size.width(), size.height())
        self._apply_button_labels(self.session.snapshot().is_paused)

    def _copy_ip(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.session.info.ip)
            self.notify.emit(f"已复制 {self.session.info.ip}")

    def save_snapshot(self) -> None:
        _, frame = self.session.latest_frame()
        if frame is None:
            self.notify.emit("当前没有画面可抓拍")
            return
        try:
            os.makedirs(self._snapshot_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            name = self.session.info.display_name().replace(" ", "_")
            path = os.path.join(self._snapshot_dir, f"{name}_{stamp}.jpg")
            with open(path, "wb") as handle:
                handle.write(frame)
            self.notify.emit(f"已保存抓拍：{path}")
        except OSError as exc:
            self.notify.emit(f"抓拍保存失败：{exc}")


def _pictures_dir() -> str:
    from PySide6.QtCore import QStandardPaths

    location = QStandardPaths.writableLocation(QStandardPaths.PicturesLocation)
    return location or os.path.expanduser("~")
