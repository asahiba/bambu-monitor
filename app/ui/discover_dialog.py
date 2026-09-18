"""局域网自动搜索对话框。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..bambu.discovery import DiscoveryService, merge_devices
from ..bambu.models import PrinterInfo
from . import theme


class _DiscoveryBridge(QObject):
    found = Signal(object)
    finished = Signal()


class DiscoverDialog(QDialog):
    """自动搜索局域网内的拓竹打印机，勾选后加入监控墙。"""

    def __init__(
        self,
        known: Optional[list[PrinterInfo]] = None,
        timeout: float = 20.0,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("自动搜索打印机")
        self.resize(900, 560)
        self._known_list: list[PrinterInfo] = list(known or [])
        # 已保存的打印机同时按「序列号」和「IP」登记：配置文件里可能只留了其中
        # 一个，用单键查会漏判「已添加」（同一台设备就被重复加进监控墙了）。
        self._known: dict[str, PrinterInfo] = {}
        for info in self._known_list:
            if info.serial:
                self._known[info.serial] = info
            if info.ip:
                self._known[info.ip] = info
        self._service: Optional[DiscoveryService] = None
        self._bridge = _DiscoveryBridge()
        self._bridge.found.connect(self._add_row)
        self._bridge.finished.connect(self._on_service_finished)
        self._remaining = int(timeout)
        self._round = 0
        self._closing = False
        # 累积的设备记录（去重交给 discovery.merge_devices）与「记录 -> 行号」映射。
        # PrinterInfo 是普通 dataclass（没有 __hash__），所以用 id() 当键；对象本身
        # 由 self._records 强引用，id 不会被回收后复用。
        self._records: list[PrinterInfo] = []
        self._row_of: dict[int, int] = {}
        self._saved_rows: set[int] = set()
        self.selected: list[PrinterInfo] = []

        layout = QVBoxLayout(self)
        header = QLabel(
            "正在搜索局域网内的拓竹打印机（逐网卡 SSDP 组播 + 网段单播扫描 + 2021 端口广播）。\n"
            "打印机对搜索有节流，不会每次都回应，所以搜索会反复进行、设备会陆续出现；"
            "列表里带「已添加」的是本机已保存的打印机，不会因为本轮没搜到而消失。\n"
            "正常情况下 30 秒内可以搜到全部设备；若始终搜不到，请确认电脑与打印机在同一网段，"
            "或使用「添加打印机」手动填写 IP。"
        )
        header.setWordWrap(True)
        header.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(header)

        self.progress = QProgressBar()
        self.progress.setRange(0, max(1, int(timeout)))
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        layout.addWidget(self.progress)

        self.table = QTableWidget(0, 6, self)
        self.table.setHorizontalHeaderLabels(
            ["选择", "名称", "机型", "IP 地址", "序列号", "访问代码（可编辑）"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.Stretch)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(5, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.rescan_button = QPushButton("重新搜索")
        self.rescan_button.clicked.connect(lambda: self._start_service(clear=True))
        row.addWidget(self.rescan_button)
        self.continuous_box = QCheckBox("持续搜索（反复扫描，直到加入监控墙）")
        self.continuous_box.setChecked(True)
        row.addWidget(self.continuous_box)
        select_all = QPushButton("全选新发现")
        select_all.clicked.connect(lambda: self._set_all(True, only_new=True))
        row.addWidget(select_all)
        select_none = QPushButton("全不选")
        select_none.clicked.connect(lambda: self._set_all(False))
        row.addWidget(select_none)
        row.addStretch(1)
        self.status_label = QLabel("搜索中…")
        self.status_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        row.addWidget(self.status_label)
        layout.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("加入监控墙")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._on_tick)

        # 先把本机已保存的打印机列出来（默认不勾选），避免「只搜到一部分」时看不到全貌
        for info in self._known_list:
            self._add_row(info, saved=True)

        self._start_service(clear=False)

    # ------------------------------------------------------------------ 搜索控制
    def _start_service(self, clear: bool = True) -> None:
        if clear:
            self.table.setRowCount(0)
            self._records.clear()
            self._row_of.clear()
            self._saved_rows.clear()
            for info in self._known_list:
                self._add_row(info, saved=True)
        self._round += 1
        self._remaining = int(self._service.timeout if self._service is not None else self._remaining)
        self._remaining = max(self._remaining, 8)
        self.progress.setRange(0, self._remaining)
        self.progress.setValue(0)
        self.rescan_button.setEnabled(False)
        if self._service is not None:
            self._service.stop()
            self._service = None
        self._service = DiscoveryService(
            self._on_found, self._on_finished, timeout=float(self._remaining)
        )
        self._service.start()
        self._tick.start()
        self.status_label.setText(f"第 {self._round} 轮搜索中…")

    def _on_tick(self) -> None:
        self.progress.setValue(self.progress.value() + 1)
        left = max(0, self._remaining - self.progress.value())
        total = len(self._records)
        self.status_label.setText(
            f"第 {self._round} 轮 · 剩余 {left} 秒 · 列表共 {total} 台（本次已新发现 "
            f"{total - len(self._saved_rows)} 台）"
        )
        if self.progress.value() >= self._remaining:
            self._tick.stop()

    def _on_service_finished(self) -> None:
        self._tick.stop()
        self.rescan_button.setEnabled(True)
        if self._closing or not self.isVisible():
            return
        if self.continuous_box.isChecked():
            # 持续搜索：立刻开始下一轮，列表与勾选状态保持不变
            self.status_label.setText(f"第 {self._round} 轮结束，继续搜索…")
            QTimer.singleShot(400, lambda: self._start_service(clear=False))
            return
        count = self.table.rowCount()
        self.status_label.setText(f"搜索结束：列表共 {count} 台" if count else "搜索结束：未发现打印机")

    # ------------------------------------------------------------------ 线程回调
    def _on_found(self, info: PrinterInfo) -> None:
        self._bridge.found.emit(info)

    def _on_finished(self) -> None:
        self._bridge.finished.emit()

    # ------------------------------------------------------------------ 列表
    def _add_row(self, info: PrinterInfo, saved: bool = False) -> None:
        """登记一台设备（已保存的或本轮搜到的），必要时新建表格行。

        去重交给 :func:`discovery.merge_devices`：它同时按序列号与 IP 查，
        能识别出「一条记录带序列号、另一条只认得出 IP」的同一台设备。以前这里
        只用 ``serial or ip`` 单键查，同一台设备会占两行 —— 用户看到的就是
        「搜索列表里同一台打印机出现了两次，只有一行能填访问代码」。
        """
        if not (info.serial or info.ip):
            return
        before = len(self._records)
        merged = merge_devices(self._records, [info])
        self._records = merged
        record = merged[-1] if len(merged) > before else _matched_record(merged, info)
        if record is None:
            return
        known = self._known_for(record)
        row = self._row_of.get(id(record))
        if row is not None:
            # 本轮又扫到了同一台：补全缺失信息（机型、序列号、名称），不新建行
            self._fill_row(row, record, known)
            if saved or known is not None:
                self._saved_rows.add(row)
            return
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._row_of[id(record)] = row
        is_saved = saved or known is not None
        if is_saved:
            self._saved_rows.add(row)

        check = QCheckBox()
        check.setChecked(not is_saved)  # 已保存的默认不勾选，避免重复添加
        holder = QWidget()
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setAlignment(Qt.AlignCenter)
        holder_layout.addWidget(check)
        self.table.setCellWidget(row, 0, holder)

        self._fill_row(row, record, known)

    def _known_for(self, record: PrinterInfo) -> Optional[PrinterInfo]:
        """查这台设备是否已在配置里（序列号、IP 双键）。"""
        return self._known_by(record.serial, record.ip)

    def _known_by(self, serial: str, ip: str) -> Optional[PrinterInfo]:
        if serial and serial in self._known:
            return self._known[serial]
        if ip and ip in self._known:
            return self._known[ip]
        return None

    def _row_box(self, row: int) -> Optional[QCheckBox]:
        holder = self.table.cellWidget(row, 0)
        return holder.findChild(QCheckBox) if holder is not None else None

    def _row_of_record(self, record: PrinterInfo) -> Optional[int]:
        return self._row_of.get(id(record))

    def _fill_row(self, row: int, info: PrinterInfo, known: Optional[PrinterInfo]) -> None:
        name = info.name or (known.name if known else "")
        if known:
            name = f"{name}（已添加）" if name else "（已添加）"
        values = [
            name,
            info.model.label if info.model.is_known else "",
            info.ip,
            info.serial,
            (known.access_code if known else "") or "",
        ]
        for column, value in enumerate(values, start=1):
            item = self.table.item(row, column)
            if item is None:
                item = QTableWidgetItem("")
                self.table.setItem(row, column, item)
            # 不覆盖用户已经填好的内容
            if value and (column == 5 or not item.text()):
                item.setText(str(value))
            if column != 5:
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)

    def _set_all(self, checked: bool, only_new: bool = False) -> None:
        for record in self._records:
            row = self._row_of_record(record)
            if row is None:
                continue
            if only_new and row in self._saved_rows:
                continue
            box = self._row_box(row)
            if box is not None:
                box.setChecked(checked)

    def _accept(self) -> None:
        selected: list[PrinterInfo] = []
        for record in self._records:
            row = self._row_of_record(record)
            if row is None:
                continue
            box = self._row_box(row)
            if box is None or not box.isChecked():
                continue
            model_item = self.table.item(row, 2)
            model = _model_from_label(model_item.text() if model_item else "")
            known = self._known_by(
                (self.table.item(row, 4).text() if self.table.item(row, 4) else ""),
                (self.table.item(row, 3).text() if self.table.item(row, 3) else ""),
            )
            if known is not None and known.model.is_known:
                model = known.model
            info = PrinterInfo(
                ip=(self.table.item(row, 3).text() if self.table.item(row, 3) else ""),
                serial=(self.table.item(row, 4).text() if self.table.item(row, 4) else ""),
                name=(self.table.item(row, 1).text() if self.table.item(row, 1) else "").replace(
                    "（已添加）", ""
                ),
                model=model,
                access_code=(self.table.item(row, 5).text() if self.table.item(row, 5) else ""),
                discovered=True,
            )
            selected.append(info)
        self.selected = selected
        self.accept()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closing = True
        self._tick.stop()
        if self._service is not None:
            self._service.stop()
        super().closeEvent(event)


def _matched_record(records: list[PrinterInfo], info: PrinterInfo) -> Optional[PrinterInfo]:
    """在 ``merge_devices`` 的结果里找出「吸收」了 ``info`` 的那条记录。

    只有当 ``info`` 与已有记录合并（而不是被当作新设备追加到末尾）时才会用到：
    `merge_devices` 就地补全已有记录（不会新建对象），所以按序列号/IP 反查即可。
    """
    for record in records:
        if record is info:
            continue
        if (info.serial and record.serial == info.serial) or (info.ip and record.ip == info.ip):
            return record
    return None


def _model_from_label(label: str):
    from ..bambu.models import PrinterModel

    for model in PrinterModel:
        if model.label == label:
            return model
    return PrinterModel.UNKNOWN
