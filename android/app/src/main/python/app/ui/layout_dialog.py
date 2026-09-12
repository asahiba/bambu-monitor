"""画面布局对话框：调整每一路画面的顺序与大小。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme

SPAN_OPTIONS = [
    ("普通（1 格）", 1),
    ("重点（2×2 格）", 2),
    ("超大（3×3 格）", 3),
]


class LayoutDialog(QDialog):
    """列表式调整：顺序 + 每路大小（例如让 X2D 占大格，其余占小格）。"""

    def __init__(self, tiles: list, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("画面布局：顺序与大小")
        self.resize(620, 520)
        self._tiles = list(tiles)

        layout = QVBoxLayout(self)
        hint = QLabel(
            "用「上移 / 下移」调整顺序（也可以直接拖动行），用右侧下拉框设置每路画面的大小。\n"
            "重点画面会占 2×2 格，适合盯住主力机型；其余画面会自动填满剩下的位置。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(hint)

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["画面", "机型 / IP", "大小"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setDragDropMode(QAbstractItemView.InternalMove)
        self.table.setDragDropOverwriteMode(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)

        buttons_row = QHBoxLayout()
        for text, handler in (
            ("↑ 上移", lambda: self._move(-1)),
            ("↓ 下移", lambda: self._move(1)),
            ("置顶", lambda: self._move(-999)),
            ("置底", lambda: self._move(999)),
        ):
            button = QPushButton(text)
            button.clicked.connect(handler)
            buttons_row.addWidget(button)
        buttons_row.addStretch(1)
        layout.addLayout(buttons_row)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("应用")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(self._apply)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

        self._fill()

    # ------------------------------------------------------------------ 内部
    def _fill(self) -> None:
        self.table.setRowCount(0)
        for tile in self._tiles:
            info = tile.session.info
            row = self.table.rowCount()
            self.table.insertRow(row)
            name_item = QTableWidgetItem(info.display_name())
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 0, name_item)

            sub_item = QTableWidgetItem(f"{info.model.label} · {info.ip}")
            sub_item.setFlags(sub_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 1, sub_item)

            combo = QComboBox()
            for text, value in SPAN_OPTIONS:
                combo.addItem(text, value)
            index = combo.findData(max(1, min(3, info.tile_span)))
            combo.setCurrentIndex(max(0, index))
            self.table.setCellWidget(row, 2, combo)

    def _move(self, delta: int) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        tile = self._tiles.pop(row)
        target = 0 if delta < 0 and abs(delta) > 1 else len(self._tiles)
        if abs(delta) > 1:
            new_row = target
        else:
            new_row = max(0, min(len(self._tiles), row + delta))
        self._tiles.insert(new_row, tile)
        self._fill()
        self.table.selectRow(new_row)

    def _apply(self) -> None:
        for row in range(self.table.rowCount()):
            combo = self.table.cellWidget(row, 2)
            if combo is not None:
                self._tiles[row].session.info.tile_span = int(combo.currentData())
        # 表格拖动排序后，行顺序可能和 _tiles 不一致，这里按表格顺序回读
        self.accept()

    @property
    def ordered_tiles(self) -> list:
        """按表格当前顺序返回画面列表。"""
        ordered = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None:
                continue
            name = item.text()
            for tile in self._tiles:
                if tile.session.info.display_name() == name and tile not in ordered:
                    ordered.append(tile)
                    break
        for tile in self._tiles:
            if tile not in ordered:
                ordered.append(tile)
        return ordered
