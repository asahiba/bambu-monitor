"""HMS 提示详情对话框：显示错误码、中文说明与官方 wiki 链接。"""

from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..bambu.hms import data_available
from ..bambu.models import HmsItem
from . import theme


class HmsDialog(QDialog):
    """列出某台打印机的全部 HMS 提示。"""

    def __init__(
        self,
        items: Sequence[HmsItem],
        printer_name: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"HMS 提示 · {printer_name}" if printer_name else "HMS 提示")
        self.resize(680, 460)

        layout = QVBoxLayout(self)
        header = QLabel(
            f"共 {len(items)} 条提示。列表中带说明页的可以直接点击查看官方解释。"
            if data_available()
            else f"共 {len(items)} 条提示（本地缺少文案表，仅显示错误码）。"
        )
        header.setStyleSheet(f"color: {theme.TEXT_DIM};")
        header.setWordWrap(True)
        layout.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(4, 4, 4, 4)
        container_layout.setSpacing(8)

        for index, item in enumerate(items, start=1):
            card = QWidget()
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 8, 10, 8)
            card_layout.setSpacing(4)
            card.setStyleSheet(
                f"background: {theme.PANEL}; border: 1px solid {theme.BORDER}; border-radius: 5px;"
            )

            code = QLabel(f"{index}. {item.short_code or f'{item.attr:08X}{item.code:08X}'}")
            code.setStyleSheet(f"color: {theme.ERROR}; font-weight: bold;")
            card_layout.addWidget(code)

            text = QLabel(item.text or "（本地文案表里没有这条错误码，请点下面的说明页查看）")
            text.setWordWrap(True)
            text.setStyleSheet(f"color: {theme.TEXT};")
            card_layout.addWidget(text)

            link = QLabel(f'<a href="{item.wiki}" style="color:{theme.ACCENT};">查看官方说明 →</a>')
            link.setOpenExternalLinks(True)
            link.setTextInteractionFlags(Qt.TextBrowserInteraction)
            card_layout.addWidget(link)

            container_layout.addWidget(card)
        container_layout.addStretch(1)
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
