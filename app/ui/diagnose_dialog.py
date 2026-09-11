"""通道诊断对话框：逐项检查某台打印机的遥测与视频通道并给出报告。"""

from __future__ import annotations

import base64
import socket
import time
from typing import Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..bambu import tlsutil
from ..bambu.camera import CameraStream
from ..bambu.models import PrinterInfo
from ..bambu.probe import probe_printer
from . import theme


class _DiagThread(QThread):
    line = Signal(str)

    def __init__(self, info: PrinterInfo, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.info = info

    def _emit(self, text: str) -> None:
        self.line.emit(text)

    def run(self) -> None:  # noqa: D102
        info = self.info
        code = info.access_code
        self._emit(f"打印机：{info.display_name()}  {info.ip}")
        self._emit(f"机型：{info.model.label}   序列号：{info.serial or '（未知）'}")
        self._emit(f"访问代码：{'已填写（' + str(len(code)) + ' 位）' if code else '未填写'}")
        self._emit("")
        if not code:
            self._emit("✗ 未填写访问代码，无法继续。请在「编辑打印机」里填写后重试。")
            return

        # 1. 端口可达性
        self._emit("① 端口连通性")
        for port, name in ((8883, "MQTT 遥测"), (6000, "JPEG 画面"), (322, "RTSPS 画面")):
            ok = self._tcp(port)
            self._emit(f"   {port:>5} {name}: {'可连接 ✓' if ok else '不可达 ✗'}")

        # 2. TLS 参数
        self._emit("")
        self._emit("② TLS 参数（证书链 + 安全级别）")
        for port in (8883, 6000, 322):
            try:
                sock, verified = tlsutil.connect_tls(info.ip, port, timeout=4.0)
                sock.close()
                self._emit(f"   {port:>5}: 握手成功 ✓  证书链{'已校验' if verified else '未校验'}")
            except Exception as exc:  # noqa: BLE001
                self._emit(f"   {port:>5}: 握手失败 ✗  {exc.__class__.__name__}: {exc}")

        # 3. 6000 端口摄像头协议
        self._emit("")
        self._emit("③ 6000 端口画面（80 字节鉴权包 + JPEG 帧）")
        camera = CameraStream(info.ip, code, serial=info.serial)
        camera.start()
        frame = camera.wait_first_frame(12.0)
        camera.stop()
        if frame:
            self._emit(f"   成功 ✓  取得 {len(frame) // 1024} KB 画面，状态：{camera.detail}")
        else:
            self._emit(f"   失败 ✗  状态：{camera.state} —— {camera.detail}")

        # 4. RTSPS(322)
        self._emit("")
        self._emit("④ RTSPS(322) 握手与鉴权")
        self._rtsp_describe()

        # 5. 遥测
        self._emit("")
        self._emit("⑤ MQTT 遥测(8883)")
        result = probe_printer(
            info.ip,
            code,
            serial=info.serial,
            timeout=10.0,
            check_camera=False,
            on_step=self._emit,
        )
        for text in result.summary().splitlines():
            self._emit("   " + text)

        self._emit("")
        self._emit("诊断结束。若画面失败但 322 端口提示 401 Unauthorized，")
        self._emit("说明 RTSP 服务是活的，请确认打印机已开启「局域网实时画面」。")

    def _tcp(self, port: int) -> bool:
        try:
            with socket.create_connection((self.info.ip, port), timeout=3.0):
                return True
        except OSError:
            return False

    def _rtsp_describe(self) -> None:
        try:
            tls, verified = tlsutil.connect_tls(
                self.info.ip, 322, timeout=5.0, server_hostname=self.info.ip
            )
        except Exception as exc:  # noqa: BLE001
            self._emit(f"   TLS 连接失败 ✗  {exc}")
            return
        try:
            auth = base64.b64encode(f"bblp:{self.info.access_code}".encode()).decode()
            request = (
                f"DESCRIBE rtsps://{self.info.ip}:322/streaming/live/1 RTSP/1.0\r\n"
                "CSeq: 1\r\n"
                "Accept: application/sdp\r\n"
                f"Authorization: Basic {auth}\r\n\r\n"
            )
            tls.sendall(request.encode())
            tls.settimeout(6.0)
            data = b""
            started = time.time()
            while b"\r\n\r\n" not in data and time.time() - started < 6:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                data += chunk
            text = data.decode("utf-8", "replace")
            head = text.splitlines()[0] if text else "（无响应）"
            self._emit(f"   DESCRIBE -> {head}（证书链{'已校验' if verified else '未校验'}）")
            for line in text.splitlines()[1:8]:
                if line.strip():
                    self._emit(f"      {line}")
            if "401" in head:
                self._emit("   注：401 表示 RTSP 服务在运行，但用户名/口令未被接受")
            elif "200" in head:
                self._emit("   RTSP 服务正常，可以直接拉流 ✓")
        except (OSError, socket.timeout, TimeoutError) as exc:
            self._emit(f"   DESCRIBE 失败 ✗  {exc}")
        finally:
            try:
                tls.close()
            except OSError:
                pass


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
        from PySide6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.output.toPlainText())
            self.copy_button.setText("已复制")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._thread.isRunning():
            self._thread.wait(3000)
        super().closeEvent(event)
