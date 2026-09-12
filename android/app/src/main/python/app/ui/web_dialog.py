"""网页监控地址提示对话框。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import theme


class WebInfoDialog(QDialog):
    """显示网页监控的访问地址，方便手机扫码/输入。"""

    def __init__(self, urls: list[str], port: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("网页监控已开启")
        self.resize(620, 340)
        self.urls = urls

        layout = QVBoxLayout(self)
        title = QLabel("在手机或另一台电脑的浏览器里打开下面的地址即可观看：")
        title.setStyleSheet(f"color: {theme.TEXT};")
        layout.addWidget(title)

        hint = QLabel(
            "· 手机需与本机在同一局域网（Wi-Fi）；地址里的 token 是访问口令，请勿外传。\n"
            "· 首次访问若连不上，请在 Windows 防火墙弹窗里勾选「专用网络」允许。\n"
            "· 网页端帧率与画质可在软件里调整；只有真正打开的页面才会占用转码开销。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(hint)

        for index, url in enumerate(urls):
            row = QHBoxLayout()
            label = QLabel("本机" if index == 0 else "局域网")
            label.setFixedWidth(48)
            label.setStyleSheet(f"color: {theme.TEXT_DIM};")
            row.addWidget(label)
            field = QLineEdit(url)
            field.setReadOnly(True)
            field.setCursorPosition(0)
            row.addWidget(field, 1)
            button = QPushButton("复制")
            button.clicked.connect(lambda _=False, value=url: self._copy(value))
            row.addWidget(button)
            layout.addLayout(row)

        note = QLabel(
            f"端口 {port}。若想更换端口或关闭网页服务，可在工具栏再次点击「🌐 网页监控」。"
        )
        note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("知道了")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _copy(self, value: str) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(value)
