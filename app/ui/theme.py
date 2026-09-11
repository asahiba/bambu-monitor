"""界面主题（深色监控风格）。"""

from __future__ import annotations

BG = "#0d1114"
PANEL = "#151c21"
PANEL_ALT = "#1b242a"
BORDER = "#26333a"
TEXT = "#e6edf1"
TEXT_DIM = "#8fa3ad"
ACCENT = "#25c2d6"
OK = "#2ecc71"
WARN = "#f5a623"
ERROR = "#ff5d5d"
PRINTING = "#25c2d6"

FONT_FAMILY = "Microsoft YaHei UI"

STYLESHEET = f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: "{FONT_FAMILY}", "Microsoft YaHei", sans-serif;
    font-size: 12px;
}}
QMainWindow::separator {{ background: {BORDER}; width: 1px; height: 1px; }}

QToolBar {{
    background: {PANEL};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 8px;
    spacing: 6px;
}}
QToolBar QToolButton {{
    background: {PANEL_ALT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 6px 12px;
    color: {TEXT};
}}
QToolBar QToolButton:hover {{ background: #23303a; border-color: {ACCENT}; }}
QToolBar QToolButton:pressed {{ background: #0f171c; }}
QToolBar QToolButton:disabled {{ color: #5a6a72; }}

QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{
    background: {PANEL_ALT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{
    background: {PANEL_ALT};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
}}

QPushButton {{
    background: {PANEL_ALT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 6px 14px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:default {{ background: {ACCENT}; color: #04222a; border-color: {ACCENT}; font-weight: bold; }}

QScrollArea {{ border: none; }}
QScrollBar:vertical {{ background: {PANEL}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #2c3a43; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: {PANEL}; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #2c3a43; border-radius: 5px; min-width: 30px; }}

QStatusBar {{ background: {PANEL}; border-top: 1px solid {BORDER}; color: {TEXT_DIM}; }}
QStatusBar::item {{ border: none; }}

QTableWidget {{
    background: {PANEL};
    gridline-color: {BORDER};
    border: 1px solid {BORDER};
    selection-background-color: #1d3c46;
}}
QHeaderView::section {{
    background: {PANEL_ALT};
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 6px;
}}
QTableWidget QLineEdit {{ border: none; background: transparent; }}

QDialog {{ background: {BG}; }}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 14px;
    padding-top: 10px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {TEXT_DIM}; }}

QProgressBar {{
    background: #101a1f;
    border: 1px solid {BORDER};
    border-radius: 5px;
    height: 12px;
    text-align: center;
    color: {TEXT};
    font-size: 10px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QMenu {{ background: {PANEL_ALT}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item:selected {{ background: {ACCENT}; color: #04222a; }}
QCheckBox::indicator {{ width: 14px; height: 14px; }}
QToolTip {{ background: {PANEL_ALT}; color: {TEXT}; border: 1px solid {ACCENT}; }}
"""
