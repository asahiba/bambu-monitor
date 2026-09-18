"""手动添加 / 编辑打印机对话框（含「测试连接」）。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..bambu.models import PrinterInfo, PrinterModel
from ..bambu.probe import ProbeResult, probe_printer
from ..bambu.timeouts import PROBE_MQTT_TIMEOUT
from . import theme
from .qt_threads import retire_thread

STREAM_MODES = [
    ("自动（推荐）", "auto"),
    ("6000 端口 JPEG 流", "tcp6000"),
    ("RTSPS 322（X1/H2/X2D）", "rtsp"),
]


class _ProbeAborted(Exception):
    """用户关掉了对话框：让探测线程在步骤边界立刻收工。"""


class _ProbeThread(QThread):
    step = Signal(str)
    done = Signal(object)

    def __init__(self, ip: str, code: str, serial: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._ip = ip
        self._code = code
        self._serial = serial
        self._aborted = False

    def stop(self) -> None:
        """请求尽快结束（在下一个步骤边界生效）。"""
        self._aborted = True

    def _on_step(self, text: str) -> None:
        if self._aborted:
            raise _ProbeAborted
        self.step.emit(text)

    def run(self) -> None:  # noqa: D102
        try:
            result = probe_printer(
                self._ip,
                self._code,
                serial=self._serial,
                timeout=PROBE_MQTT_TIMEOUT,
                on_step=self._on_step,
                should_stop=lambda: self._aborted,
            )
        except _ProbeAborted:
            return
        if self._aborted:
            return
        self.done.emit(result)


class PrinterEditDialog(QDialog):
    """添加或编辑一台打印机。"""

    def __init__(self, parent: Optional[QWidget] = None, printer: Optional[PrinterInfo] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑打印机" if printer else "添加打印机")
        self.setMinimumWidth(520)
        self.printer = printer or PrinterInfo()
        self._thread: Optional[_ProbeThread] = None
        self.result: Optional[PrinterInfo] = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        self.name_edit = QLineEdit(self.printer.name)
        self.name_edit.setPlaceholderText("例如：车间 A1 / 二号机（可留空）")
        form.addRow("名称", self.name_edit)

        self.ip_edit = QLineEdit(self.printer.ip)
        self.ip_edit.setPlaceholderText("打印机 IP，例如 192.168.1.50")
        form.addRow("IP 地址", self.ip_edit)

        self.code_edit = QLineEdit(self.printer.access_code)
        self.code_edit.setPlaceholderText("打印机屏幕：设置 → 网络 → 局域网访问代码")
        form.addRow("访问代码", self.code_edit)

        self.serial_edit = QLineEdit(self.printer.serial)
        self.serial_edit.setPlaceholderText("可留空，测试连接时自动识别")
        form.addRow("序列号", self.serial_edit)

        self.model_combo = QComboBox()
        self.model_combo.addItem("自动识别", PrinterModel.UNKNOWN.value)
        for model in PrinterModel:
            if model is PrinterModel.UNKNOWN:
                continue
            self.model_combo.addItem(model.label, model.value)
        index = self.model_combo.findData(self.printer.model.value)
        self.model_combo.setCurrentIndex(max(0, index))
        form.addRow("机型", self.model_combo)

        self.stream_combo = QComboBox()
        for label, value in STREAM_MODES:
            self.stream_combo.addItem(label, value)
        index = self.stream_combo.findData(self.printer.stream_mode or "auto")
        self.stream_combo.setCurrentIndex(max(0, index))
        form.addRow("视频通道", self.stream_combo)
        layout.addLayout(form)

        hint = QLabel(
            "提示：打印机需与电脑在同一局域网，并已开启「局域网模式」。"
            "访问代码只保存在本机并加密（Windows 用 DPAPI，其它平台用本机密钥），"
            "不会上传到任何服务器。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(hint)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(96)
        self.log.setPlaceholderText("点击「测试连接」验证 IP 与访问代码…")
        layout.addWidget(self.log)

        test_row = QHBoxLayout()
        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(self._start_test)
        test_row.addWidget(self.test_button)
        self.auto_serial = QCheckBox("自动识别序列号")
        self.auto_serial.setChecked(True)
        test_row.addWidget(self.auto_serial)
        test_row.addStretch(1)
        layout.addLayout(test_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ 测试连接
    def _start_test(self) -> None:
        ip = self.ip_edit.text().strip()
        code = self.code_edit.text().strip()
        if not ip:
            QMessageBox.warning(self, "缺少信息", "请先填写打印机 IP 地址。")
            return
        if not code:
            QMessageBox.warning(self, "缺少信息", "请填写打印机屏幕上的局域网访问代码。")
            return
        self.log.clear()
        self.test_button.setEnabled(False)
        self.test_button.setText("测试中…")
        self._thread = _ProbeThread(ip, code, self.serial_edit.text().strip(), self)
        self._thread.step.connect(self.log.appendPlainText)
        self._thread.done.connect(self._on_test_done)
        self._thread.start()

    def _on_test_done(self, result: ProbeResult) -> None:
        self.test_button.setEnabled(True)
        self.test_button.setText("测试连接")
        self.log.appendPlainText("")
        self.log.appendPlainText(result.summary())
        if result.serial and self.auto_serial.isChecked():
            self.serial_edit.setText(result.serial)
        if result.firmware and self.model_combo.currentData() == PrinterModel.UNKNOWN.value:
            index = self.model_combo.findData(result.model.value)
            if index > 0:
                self.model_combo.setCurrentIndex(index)
        if not result.ok:
            QMessageBox.warning(
                self,
                "测试未通过",
                "既没有取到画面也没有取到状态。请检查：\n"
                "• 打印机是否开启「局域网模式」\n"
                "• IP 是否可达（能否 ping 通）\n"
                "• 访问代码是否正确（打印机屏幕 → 设置 → 网络）",
            )

    # ------------------------------------------------------------------ 保存
    def _accept(self) -> None:
        ip = self.ip_edit.text().strip()
        if not ip:
            QMessageBox.warning(self, "缺少信息", "请填写打印机 IP 地址。")
            return
        model_value = self.model_combo.currentData()
        info = PrinterInfo(
            ip=ip,
            serial=self.serial_edit.text().strip().upper(),
            name=self.name_edit.text().strip(),
            access_code=self.code_edit.text().strip(),
            model=PrinterModel(model_value),
            stream_mode=self.stream_combo.currentData(),
            firmware=self.printer.firmware,
        )
        if info.model is PrinterModel.UNKNOWN and info.serial:
            from ..bambu.models import detect_model

            info.model = detect_model(info.serial)
        self.result = info
        self.accept()

    def closeEvent(self, event) -> None:  # noqa: N802
        thread = self._thread
        if thread is not None and thread.isRunning():
            thread.stop()
            if not thread.wait(2000):
                # 还卡在 TCP 超时里：**不能让它随对话框被销毁**（运行中的 QThread
                # 被析构会让 Qt fail-fast，整个程序消失）。交给退休名单，
                # 由它保证进程退出前等线程结束。
                retire_thread(thread)
                self._thread = None
        super().closeEvent(event)
