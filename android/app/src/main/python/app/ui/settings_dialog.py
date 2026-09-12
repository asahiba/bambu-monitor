"""设置对话框：帧率、刷新与网页监控参数。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig
from . import theme

FPS_OPTIONS = [
    ("省电（4 fps）", 4.0),
    ("标准（8 fps）", 8.0),
    ("流畅（10 fps）", 10.0),
    ("很流畅（15 fps）", 15.0),
    ("不限（按打印机实际帧率）", 0.0),
]

REFRESH_OPTIONS = [
    ("省电（300 ms）", 300),
    ("标准（150 ms）", 150),
    ("流畅（100 ms）", 100),
]

WEB_FPS_OPTIONS = [
    ("省流量（2 fps）", 2.0),
    ("标准（4 fps）", 4.0),
    ("流畅（8 fps）", 8.0),
]


class SettingsDialog(QDialog):
    """修改会影响 CPU 占用与网页体验的参数。"""

    def __init__(self, config: AppConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(460, 380)
        self.config = config

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.fps_combo = QComboBox()
        for text, value in FPS_OPTIONS:
            self.fps_combo.addItem(text, value)
        index = self.fps_combo.findData(float(config.max_fps))
        self.fps_combo.setCurrentIndex(index if index >= 0 else 2)
        form.addRow("每路最大帧率", self.fps_combo)

        self.refresh_combo = QComboBox()
        for text, value in REFRESH_OPTIONS:
            self.refresh_combo.addItem(text, value)
        index = self.refresh_combo.findData(int(config.refresh_ms))
        self.refresh_combo.setCurrentIndex(index if index >= 0 else 1)
        form.addRow("界面刷新间隔", self.refresh_combo)

        self.web_port = QSpinBox()
        self.web_port.setRange(1, 65535)
        self.web_port.setValue(int(config.web_port))
        form.addRow("网页端口", self.web_port)

        self.web_fps_combo = QComboBox()
        for text, value in WEB_FPS_OPTIONS:
            self.web_fps_combo.addItem(text, value)
        index = self.web_fps_combo.findData(float(config.web_fps))
        self.web_fps_combo.setCurrentIndex(index if index >= 0 else 1)
        form.addRow("网页帧率", self.web_fps_combo)

        self.web_width = QSpinBox()
        self.web_width.setRange(240, 1920)
        self.web_width.setSingleStep(80)
        self.web_width.setValue(int(config.web_max_width))
        self.web_width.setSuffix(" px")
        form.addRow("网页画面最大宽度", self.web_width)

        layout.addLayout(form)

        hint = QLabel(
            "· 帧率主要影响 CPU：RTSPS 通道解码 1080p 是主要开销，实测 8fps 时两路画面约占单核 18%，"
            "不限帧率时可达 88%。\n"
            "· P1/A1 的 6000 通道本身只有约 1fps，调高上限不会有变化。\n"
            "· 网页参数改了之后，需要重新开启一次「🌐 网页监控」才生效。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply(self) -> None:
        self.config.max_fps = float(self.fps_combo.currentData())
        self.config.refresh_ms = int(self.refresh_combo.currentData())
        self.config.web_port = int(self.web_port.value())
        self.config.web_fps = float(self.web_fps_combo.currentData())
        self.config.web_max_width = int(self.web_width.value())
        self.accept()
