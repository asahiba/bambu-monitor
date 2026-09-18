"""通道诊断对话框：逐项检查某台打印机的遥测与视频通道并给出报告。

流程本身（端口 / TLS / 6000 取帧 / RTSPS DESCRIBE / MQTT）在
`app/bambu/diagnostics.py`，与命令行工具 `tools/diagnose.py` 共用一份，
本文件只负责「在对话框里边跑边显示 + 关闭时能取消」。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..bambu import diagnostics
from ..bambu.models import PrinterInfo
from . import theme
from .qt_threads import retire_thread


class _DiagAborted(Exception):
    """用户关闭了对话框：让诊断线程在步骤边界立刻收工。"""


class _DiagThread(QThread):
    line = Signal(str)

    def __init__(self, info: PrinterInfo, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.info = info
        self._aborted = False

    def stop(self) -> None:
        """请求尽快结束（在下一个步骤边界生效）。"""
        self._aborted = True

    def _emit(self, text: str) -> None:
        # 用户点了关闭就尽快收工：诊断一轮要 20 秒以上，
        # 让线程跑完再销毁对话框是不可能的（见 closeEvent）。
        if self._aborted:
            raise _DiagAborted
        self.line.emit(text)

    def run(self) -> None:  # noqa: D102
        try:
            self._run_diagnostics()
        except _DiagAborted:
            # 直接 emit，别走 _emit（它正是抛异常的那个）
            self.line.emit("（已取消：对话框关闭，诊断未跑完）")

    def _run_diagnostics(self) -> None:
        info = self.info
        code = info.access_code
        self._emit(f"打印机：{info.display_name()}  {info.ip}")
        self._emit(f"机型：{info.model.label}   序列号：{info.serial or '（未知）'}")
        self._emit(f"访问代码：{'已填写（' + str(len(code)) + ' 位）' if code else '未填写'}")
        self._emit("")
        if not code:
            self._emit("✗ 未填写访问代码，无法继续。请在「编辑打印机」里填写后重试。")
            return

        # 流程与命令行工具 tools/diagnose.py **共用** app/bambu/diagnostics.py。
        # 以前两边各写一份，连 RTSPS 的候选路径都不一样（命令行会试 3 个路径，
        # 界面只试 1 个），于是出现过「命令行说通了、界面上说不行」这种自相矛盾。
        stream = diagnostics.run(
            info.ip,
            code,
            serial=info.serial,
            # 界面不额外做一次 RTSPS 真拉流（那一步最长 20 秒）；
            # 需要完整拉到帧请用 tools/diagnose.py
            with_rtsp_frame=False,
            should_stop=lambda: self._aborted,
            on_step=self._emit,
        )
        try:
            for event in stream:
                if isinstance(event, diagnostics.SectionStart):
                    self._emit("")
                    self._emit(event.title)
                else:
                    for line in event.lines:
                        self._emit(line)
        finally:
            # 走 _emit 抛出的 _DiagAborted 也要让生成器收尾：
            # 里面的取帧/遥测线程靠它的 finally 停干净，否则关闭对话框会留下游离线程
            stream.close()

        self._emit("")
        self._emit("诊断结束。若画面失败但 322 端口提示 401 Unauthorized，")
        self._emit("说明 RTSP 服务是活的，请确认打印机已开启「局域网实时画面」。")


class DiagnoseDialog(QDialog):
    """显示诊断过程与结论。"""

    def __init__(self, info: PrinterInfo, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"通道诊断 · {info.display_name()}")
        self.resize(760, 560)
        self.info = info

        layout = QVBoxLayout(self)
        hint = QLabel(
            "逐项检查该打印机的遥测与视频通道。报告可直接复制给开发者排查。"
        )
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(hint)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output, 1)

        row = QHBoxLayout()
        self.copy_button = QPushButton("复制报告")
        self.copy_button.clicked.connect(self._copy)
        self.copy_button.setEnabled(False)
        row.addWidget(self.copy_button)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)

        self._thread = _DiagThread(info, self)
        self._thread.line.connect(self.output.appendPlainText)
        self._thread.finished.connect(lambda: self.copy_button.setEnabled(True))
        self._thread.start()

    def _copy(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.output.toPlainText())
            self.copy_button.setText("已复制")

    def closeEvent(self, event) -> None:  # noqa: N802
        thread = self._thread
        if thread is not None and thread.isRunning():
            thread.stop()
            if not thread.wait(2000):
                # 线程还卡在某次 TCP 超时里。**绝不能让它随对话框一起被销毁**：
                # 运行中的 QThread 被析构会让 Qt 直接 fail-fast
                # （用户看到的是「点开诊断、随手关掉 → 程序整个消失」）。
                retire_thread(thread)
                self._thread = None
        super().closeEvent(event)
