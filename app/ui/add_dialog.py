"""手动添加 / 编辑打印机对话框（含「测试连接」）。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QIntValidator
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
from ..core import registry
from . import theme
from .qt_threads import retire_thread

STREAM_MODES = [
    ("自动（推荐）", "auto"),
    ("6000 端口 JPEG 流", "tcp6000"),
    ("RTSPS 322（X1/H2/X2D）", "rtsp"),
]

#: 非拓竹族「测试连接」的单次 HTTP 超时（秒）。
#: 比拓竹那套（MQTT 10 秒 + 摄像头 12 秒）短得多：一次 GET 就能判定，
#: 用户不该为了一行版本号等在对话框前面十几秒。
PROBE_HTTP_TIMEOUT = 5.0

#: klippy 状态码的中文说法。直接把 ``ready``/``shutdown`` 摆给用户看，
#: 会被当成又一个看不懂的报错。
KLIPPY_STATE_TEXT = {
    "ready": "就绪",
    "startup": "启动中",
    "shutdown": "已停机（Klipper 因错误停机，需在机器上 FIRMWARE_RESTART）",
    "error": "错误",
}


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


@dataclass
class FamilyProbeResult:
    """非拓竹族「测试连接」的结果。

    为什么不复用 `probe.ProbeResult`：它的 ``summary()`` 写死了
    「遥测（8883）/ 画面（6000）」—— 那是拓竹的端口体系，照搬到 Klipper 机器上
    会让用户以为自己的机器也该开 8883，比不给结论更糟。
    """

    ok: bool = False
    ip: str = ""
    #: 真正连上的端口（0 = 一个都没连上）
    port: int = 0
    #: 给用户看的逐行结论（就是日志框里的内容）
    lines: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return "\n".join(self.lines)


def _fetch_server_info(ip: str, port: int, api_key: str, timeout: float) -> dict:
    """GET ``http://<ip>:<port>/server/info``，返回响应里的 ``result`` 字段。

    **只读端点**：Moonraker 的 ``/server/info`` 只回答版本与 klippy 状态，
    不改变设备任何状态 —— 与「绝不对打印机下发控制命令」这条红线一致，
    所以本文件里没有任何 POST / 控制类请求。
    """
    url = f"http://{ip}:{port}/server/info"
    request = urllib.request.Request(url, method="GET")
    if api_key:
        # Moonraker 的鉴权头就是 X-Api-Key（不是 Bearer、也不是 URL 参数）
        request.add_header("X-Api-Key", api_key)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    if not body:
        return {}
    payload = json.loads(body.decode("utf-8", errors="replace"))
    result = payload.get("result") if isinstance(payload, dict) else None
    return result if isinstance(result, dict) else {}


def _describe_server_info(port: int, info: dict) -> list[str]:
    """把 ``/server/info`` 的结果翻成几行给用户看的文字。

    用户点「测试连接」要确认的是「对面真的是 Moonraker、Klipper 起没起来」，
    而不是一句笼统的「成功」。
    """
    lines = [f"Moonraker（HTTP {port}）：成功"]
    version = str(info.get("moonraker_version") or "").strip()
    if version:
        lines.append(f"Moonraker 版本：{version}")
    # Klipper 自己的版本只在部分组件组合里由 /server/info 透出：有就显示、
    # 没有就不编 —— 编一个「未知」会让用户以为探测失败了。
    klipper = str(info.get("klipper_version") or info.get("software_version") or "").strip()
    if klipper:
        lines.append(f"Klipper 版本：{klipper}")
    state = str(info.get("klippy_state") or "").strip()
    if state:
        lines.append(f"Klipper 状态：{KLIPPY_STATE_TEXT.get(state, state)}")
    if info.get("klippy_connected") is False:
        lines.append("注意：Moonraker 连不上 klippy（Klipper 服务没起来），监控不会有状态。")
    return lines


def _port_hint(descriptor: registry.FamilyDescriptor) -> str:
    """端口输入框的提示：说清「留空 = 用哪个默认端口」以及还有哪些端口常见。

    Moonraker 的形态（U1 只有 80 通、通用 Klipper 机器只有 7125 通）必须写出来，
    用户照着提示填就不用试。

    ⚠️ 措辞要跟**实际行为**一致：留空时用的是 ``default_port``（Moonraker 是 7125，
    官方默认），**不会**自动去试候选端口 —— 会话创建发生在界面线程上，
    多试一个端口就是多等一次超时。候选端口只在「测试连接」时才会依次尝试。
    """
    default = int(descriptor.default_port or 0)
    text = "留空或 0 = 用该族默认端口" + (f"（{default}）" if default else "")
    candidates = [port for port in descriptor.candidate_ports if port > 0]
    if candidates:
        order = " → ".join(str(port) for port in candidates)
        text += f"；常见端口：{order}（「测试连接」会依次试一遍）"
    return text


class _HttpProbeThread(QThread):
    """非拓竹族的「测试连接」：纯 HTTP 只读探测，同样在后台线程里跑。

    拓竹那套（MQTT 8883 + 摄像头 6000/322）对 Klipper 机器毫无意义，
    所以换成一个对 Moonraker HTTP 端点的 GET；端口的尝试顺序来自注册表。
    """

    step = Signal(str)
    done = Signal(object)

    def __init__(
        self,
        ip: str,
        ports: list[int],
        api_key: str = "",
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._ip = ip
        self._ports = [int(port) for port in ports]
        self._api_key = api_key
        self._aborted = False

    def stop(self) -> None:
        """请求尽快结束（在下一个端口之前生效）。"""
        self._aborted = True

    def run(self) -> None:  # noqa: D102
        result = FamilyProbeResult(ip=self._ip)
        for port in self._ports:
            if self._aborted:
                return
            self.step.emit(f"GET http://{self._ip}:{port}/server/info …")
            try:
                info = _fetch_server_info(self._ip, port, self._api_key, PROBE_HTTP_TIMEOUT)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                # 某个端口不通是**正常**的（Moonraker 可能只开了 80 或只开了 7125），
                # 所以记一行继续试下一个；全都试完才算失败。
                result.lines.append(f"端口 {port}：失败 —— {exc}")
                continue
            result.ok = True
            result.port = port
            result.lines.extend(_describe_server_info(port, info))
            break
        if self._aborted:
            return
        if not result.ok:
            result.lines.append(
                "结论：没有取到 Moonraker 的 /server/info（端口都不通，或对面不是 Moonraker）。"
            )
        self.done.emit(result)


class PrinterEditDialog(QDialog):
    """添加或编辑一台打印机。"""

    def __init__(self, parent: Optional[QWidget] = None, printer: Optional[PrinterInfo] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑打印机" if printer else "添加打印机")
        self.setMinimumWidth(520)
        self.printer = printer or PrinterInfo()
        self._thread: Optional[QThread] = None
        self.result: Optional[PrinterInfo] = None
        #: 每个设备族各自保存一份「用户在这个族下填的凭据」。切族时只换输入框里的内容，
        #: 不动另一族那份 —— 否则用户看完 Moonraker 再切回拓竹，刚填的访问代码就没了。
        self._credentials: dict[str, str] = {
            descriptor.family: str(getattr(self.printer, descriptor.credential.key, "") or "")
            for descriptor in registry.all_families()
        }

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        # 设备族放最上面：它决定了下面几行**是什么**（凭据那一行叫什么、端口怎么填、
        # 哪些行对这台机器根本没有意义），所以顺序必须是「先选族、再填细节」。
        self.family_combo = QComboBox()
        for descriptor in registry.all_families():
            self.family_combo.addItem(descriptor.label, descriptor.family)
        # 老配置没有 family 字段 → resolve_family 一律解析成拓竹，下拉自然停在拓竹
        self._loaded_family = registry.resolve_family(self.printer).family
        self.family_combo.setCurrentIndex(max(0, self.family_combo.findData(self._loaded_family)))
        form.addRow("设备族", self.family_combo)

        self.name_edit = QLineEdit(self.printer.name)
        self.name_edit.setPlaceholderText("例如：车间 A1 / 二号机（可留空）")
        form.addRow("名称", self.name_edit)

        self.ip_edit = QLineEdit(self.printer.ip)
        self.ip_edit.setPlaceholderText("打印机 IP，例如 192.168.1.50")
        form.addRow("IP 地址", self.ip_edit)

        # 凭据这一行的标签 / 提示 / 是否隐藏显示，全部由族的 `CredentialPolicy` 决定。
        # 以前这里写死「访问代码 + 打印机屏幕上的 8 位码」，对 Moonraker
        # （API Key，且内网默认免鉴权可留空）完全不适用。
        self.credential_label = QLabel()
        self.code_edit = QLineEdit()
        form.addRow(self.credential_label, self.code_edit)

        self.port_edit = QLineEdit()
        # 用 QIntValidator 在输入期就挡住字母：端口是数字字段，
        # 让用户敲不进去比保存时报错友好。
        self.port_edit.setValidator(QIntValidator(0, 65535, self))
        if self.printer.port > 0:
            self.port_edit.setText(str(self.printer.port))
        form.addRow("端口", self.port_edit)

        self.camera_edit = QLineEdit(self.printer.camera_url)
        self.camera_edit.setPlaceholderText(
            "留空则尝试自动发现（/server/webcams/list）；U1 这类设备需手填快照地址"
        )
        form.addRow("摄像头 URL", self.camera_edit)

        # 下面三行是**拓竹专有**的：机型/序列号来自拓竹的 MQTT 遥测与 8 位访问代码，
        # 视频通道是 6000/322 那套。Klipper 机器没有这些概念，选第三方族时要停用。
        self.serial_label = QLabel("序列号")
        self.serial_edit = QLineEdit(self.printer.serial)
        self.serial_edit.setPlaceholderText("可留空，测试连接时自动识别")
        form.addRow(self.serial_label, self.serial_edit)

        self.model_label = QLabel("机型")
        self.model_combo = QComboBox()
        self.model_combo.addItem("自动识别", PrinterModel.UNKNOWN.value)
        for model in PrinterModel:
            if model is PrinterModel.UNKNOWN:
                continue
            self.model_combo.addItem(model.label, model.value)
        index = self.model_combo.findData(self.printer.model.value)
        self.model_combo.setCurrentIndex(max(0, index))
        form.addRow(self.model_label, self.model_combo)

        self.stream_label = QLabel("视频通道")
        self.stream_combo = QComboBox()
        for label, value in STREAM_MODES:
            self.stream_combo.addItem(label, value)
        index = self.stream_combo.findData(self.printer.stream_mode or "auto")
        self.stream_combo.setCurrentIndex(max(0, index))
        form.addRow(self.stream_label, self.stream_combo)
        layout.addLayout(form)

        #: （标签, 输入控件）：选非拓竹族时整行置灰，切回拓竹立刻恢复 ——
        #: 只是停用而不是隐藏，用户仍能看见原值，也不会以为界面出错。
        self._bambu_rows: list[tuple[QLabel, QWidget]] = [
            (self.serial_label, self.serial_edit),
            (self.model_label, self.model_combo),
            (self.stream_label, self.stream_combo),
        ]
        self.bambu_only_note = QLabel(
            "Klipper / Moonraker 机器没有「机型 / 视频通道 / 序列号」这些概念"
            "（它们是拓竹 MQTT 体系的字段），下面几行已停用。"
        )
        self.bambu_only_note.setWordWrap(True)
        self.bambu_only_note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(self.bambu_only_note)

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

        # 信号连接放在最后：一旦触发就会去读写上面这些控件，控件必须先建好。
        self.family_combo.currentIndexChanged.connect(self._on_family_changed)
        self.code_edit.setText(self._credentials.get(self._loaded_family, ""))
        self._apply_family()

    # ------------------------------------------------------------------ 设备族
    def _descriptor(self) -> registry.FamilyDescriptor:
        """当前选中的设备族描述符（取不到就退回该设备的族，避免拿到 None）。"""
        return registry.get(self.family_combo.currentData()) or registry.resolve_family(
            self.printer
        )

    def _on_family_changed(self, _index: int) -> None:
        """切换设备族：先收起上一族填的凭据，再取出新族那一份。"""
        if self._loaded_family:
            self._credentials[self._loaded_family] = self.code_edit.text()
        self._loaded_family = self._descriptor().family
        self.code_edit.setText(self._credentials.get(self._loaded_family, ""))
        self._apply_family()

    def _apply_family(self) -> None:
        """按当前选中的族刷新凭据行、端口提示与拓竹专有行的可用性。"""
        descriptor = self._descriptor()
        policy = descriptor.credential
        self.credential_label.setText(policy.label)
        self.code_edit.setPlaceholderText(policy.hint)
        # secret=True 的族（拓竹访问代码、Moonraker API Key）一律用口令模式：
        # 这两样都能直接控制打印机，不该明文摆在屏幕上。
        self.code_edit.setEchoMode(QLineEdit.Password if policy.secret else QLineEdit.Normal)
        self.port_edit.setPlaceholderText(_port_hint(descriptor))

        is_bambu = descriptor.family == registry.FAMILY_BAMBU
        for label, widget in self._bambu_rows:
            label.setEnabled(is_bambu)
            widget.setEnabled(is_bambu)
        # 「自动识别序列号」也是拓竹专有：第三方族没有序列号可识别
        self.auto_serial.setEnabled(is_bambu)
        self.bambu_only_note.setVisible(not is_bambu)

    def _credential_text(self) -> str:
        """凭据输入框的内容（用户在这里只填一个框，写到哪个字段由族决定）。"""
        return self.code_edit.text().strip()

    def _port_value(self) -> int:
        """端口输入框的值；留空或 0 一律返回 0（= 用该族默认端口）。

        这里**不**把 0 换成 ``descriptor.default_port``：配置里存 0 意味着
        「跟着族默认端口走」，以后默认端口变了也能自动跟上。
        """
        text = self.port_edit.text().strip()
        if not text:
            return 0
        try:
            return max(0, int(text))
        except ValueError:
            # QIntValidator 理论上不让敲进来，这里只是兜底，别让保存时崩掉
            return 0

    def _probe_ports(self) -> list[int]:
        """「测试连接」要依次尝试的端口。

        填了端口就只试它（用户明确指定了）；没填就按族的候选顺序 ——
        Moonraker 是 80 优先、7125 回退，两个都试才不至于在 U1 上误报失败。
        """
        port = self._port_value()
        if port > 0:
            return [port]
        descriptor = self._descriptor()
        ordered = [int(item) for item in descriptor.candidate_ports if int(item) > 0]
        if not ordered and descriptor.default_port:
            ordered = [int(descriptor.default_port)]
        return ordered

    # ------------------------------------------------------------------ 测试连接
    def _start_test(self) -> None:
        ip = self.ip_edit.text().strip()
        if not ip:
            QMessageBox.warning(self, "缺少信息", "请先填写打印机 IP 地址。")
            return
        descriptor = self._descriptor()
        credential = self._credential_text()
        if descriptor.credential.required and not credential:
            # 只有该族**真的**需要凭据时才拦：Moonraker 内网默认免鉴权，
            # 强迫用户去填一个不存在的 Key 反而会让人以为必须填。
            QMessageBox.warning(self, "缺少信息", f"请填写{descriptor.credential.label}。")
            return
        self.log.clear()
        self.test_button.setEnabled(False)
        self.test_button.setText("测试中…")
        if descriptor.family == registry.FAMILY_BAMBU:
            self._thread = _ProbeThread(ip, credential, self.serial_edit.text().strip(), self)
        else:
            # 第三方族走 HTTP：探测逻辑与拓竹那套完全不同，所以换一个线程类，
            # 而不是在 _ProbeThread 里塞分支。
            self._thread = _HttpProbeThread(ip, self._probe_ports(), credential, self)
        self._thread.step.connect(self.log.appendPlainText)
        self._thread.done.connect(self._on_test_done)
        self._thread.start()

    def _on_test_done(self, result) -> None:
        self.test_button.setEnabled(True)
        self.test_button.setText("测试连接")
        self.log.appendPlainText("")
        self.log.appendPlainText(result.summary())
        if isinstance(result, ProbeResult):
            # 拓竹专有的回填：序列号 / 机型来自 MQTT 遥测，第三方族没有这些字段
            if result.serial and self.auto_serial.isChecked():
                self.serial_edit.setText(result.serial)
            if result.firmware and self.model_combo.currentData() == PrinterModel.UNKNOWN.value:
                index = self.model_combo.findData(result.model.value)
                if index > 0:
                    self.model_combo.setCurrentIndex(index)
        if result.ok:
            return
        if isinstance(result, ProbeResult):
            QMessageBox.warning(
                self,
                "测试未通过",
                "既没有取到画面也没有取到状态。请检查：\n"
                "• 打印机是否开启「局域网模式」\n"
                "• IP 是否可达（能否 ping 通）\n"
                "• 访问代码是否正确（打印机屏幕 → 设置 → 网络）",
            )
            return
        descriptor = self._descriptor()
        candidates = "、".join(str(port) for port in self._probe_ports()) or "默认端口"
        QMessageBox.warning(
            self,
            "测试未通过",
            f"没有取到 Moonraker 的 /server/info。请检查：\n"
            f"• IP 是否可达（能否 ping 通）\n"
            f"• 端口是否正确（已尝试：{candidates}）\n"
            f"• 若这台机器开了 force_logins，需要填 {descriptor.credential.label}"
            "（从 Moonraker 的 /access/api_key 获取）",
        )

    # ------------------------------------------------------------------ 保存
    def _accept(self) -> None:
        ip = self.ip_edit.text().strip()
        if not ip:
            QMessageBox.warning(self, "缺少信息", "请填写打印机 IP 地址。")
            return
        descriptor = self._descriptor()
        is_bambu = descriptor.family == registry.FAMILY_BAMBU
        info = PrinterInfo(
            ip=ip,
            name=self.name_edit.text().strip(),
            # 拓竹存空字符串（配置里"没有 family"就是拓竹，见 registry.resolve_family
            # 与网页端同一约定），第三方族写自己的 id。
            family="" if is_bambu else descriptor.family,
            port=self._port_value(),
            camera_url=self.camera_edit.text().strip(),
            firmware=self.printer.firmware,
        )
        # ⚠️ 凭据**只能**写进该族自己的字段：把 API Key 写进 access_code
        # 会让拓竹会话拿它去登录 MQTT（必然失败），也让配置里多出一个语义错误的字段。
        setattr(info, descriptor.credential.key, self._credential_text())
        if is_bambu:
            info.serial = self.serial_edit.text().strip().upper()
            info.model = PrinterModel(self.model_combo.currentData())
            info.stream_mode = self.stream_combo.currentData()
            if info.model is PrinterModel.UNKNOWN and info.serial:
                from ..bambu.models import detect_model

                info.model = detect_model(info.serial)
        else:
            # 非拓竹族：那三行是停用的，取值沿用原对象 —— 免得「编辑一次」
            # 就把用户的旧值抹成空（切回拓竹族时还要用）。
            info.serial = self.printer.serial
            info.model = self.printer.model
            info.stream_mode = self.printer.stream_mode
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
