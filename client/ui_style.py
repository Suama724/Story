"""Story 的浅色主题与共用控件样式。"""

from pathlib import Path

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


SURFACE = "#ffffff"
BACKGROUND = "#f6f6f6"
SIDEBAR = "#f0f0f0"
HOVER = "#e9e9e9"
SELECTION = "#dddddd"
BORDER = "#d4d4d4"
TEXT = "#242424"
MUTED = "#606060"
DISABLED = "#858585"
FOCUS = "#686868"
DANGER = "#a33737"
ICON_ROOT = Path(__file__).resolve().parents[1] / "assets" / "icons" / "phosphor"


def apply_theme(app: QApplication) -> None:
    # Fusion uses the same palette for controls and popups across system themes.
    app.setStyle("Fusion")
    palette = app.palette()
    role = QPalette.ColorRole
    for key, tone in (
        (role.Window, BACKGROUND), (role.WindowText, TEXT), (role.Base, SURFACE),
        (role.AlternateBase, SIDEBAR), (role.Text, TEXT), (role.PlaceholderText, MUTED),
        (role.Button, BACKGROUND), (role.ButtonText, TEXT), (role.Highlight, SELECTION),
        (role.HighlightedText, TEXT), (role.Accent, FOCUS),
        (role.Light, SURFACE), (role.Midlight, HOVER), (role.Mid, BORDER),
        (role.Dark, MUTED), (role.Shadow, TEXT), (role.BrightText, SURFACE),
        (role.ToolTipBase, SURFACE), (role.ToolTipText, TEXT),
        (role.Link, TEXT), (role.LinkVisited, MUTED),
    ):
        palette.setColor(key, QColor(tone))
    for key in (role.WindowText, role.Text, role.PlaceholderText, role.ButtonText, role.HighlightedText):
        palette.setColor(QPalette.ColorGroup.Disabled, key, QColor(DISABLED))
    app.setPalette(palette)

    info_icon = (ICON_ROOT / "bold" / "info-bold.svg").as_posix()
    app.setStyleSheet(f"""
        QMessageBox {{
            messagebox-question-icon: url("{info_icon}");
            messagebox-information-icon: url("{info_icon}");
        }}
    """)


BUTTON_STYLE = f"""
QPushButton {{ background: {BACKGROUND}; border: 1px solid {BORDER}; border-radius: 4px; }}
QPushButton:hover {{ background: {HOVER}; border-color: {DISABLED}; }}
QPushButton:pressed {{ background: {SELECTION}; }}
QPushButton:focus {{ border: 2px solid {FOCUS}; }}
QPushButton#primary {{ background: {TEXT}; color: {SURFACE}; border-color: {TEXT}; }}
QPushButton#primary:hover {{ background: #414141; }}
QPushButton#primary:pressed {{ background: #161616; }}
QPushButton#primary:focus {{ border-color: {DISABLED}; }}
QPushButton:disabled, QPushButton#primary:disabled {{
    background: {SIDEBAR}; color: {DISABLED}; border-color: {BORDER};
}}
"""

STYLE = BUTTON_STYLE + f"""
QWidget {{ color: {TEXT}; font-size: 14px; }}
QDialog {{ background: {BACKGROUND}; }}
QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; }}
QLabel#title {{ font-size: 24px; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; }}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 4px; padding: 7px;
    selection-background-color: {SELECTION}; selection-color: {TEXT};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {{ border-color: {FOCUS}; }}
QPushButton {{ padding: 8px 14px; }}
"""

CHANNEL_NAV_STYLE = f"""
QWidget#channelWorkspace, QFrame#channelSidebar {{ background: {BACKGROUND}; }}
QSplitter#channelSplitter::handle:horizontal {{ background: {BACKGROUND}; border: none; }}
QSplitter#channelSplitter::handle:horizontal:hover {{ background: {HOVER}; }}
QSplitter#channelSplitter::handle:horizontal:pressed {{ background: {SELECTION}; }}
QFrame#channelChatPanel {{
    background: {SURFACE}; border-top-left-radius: 12px; border-bottom-left-radius: 12px;
}}
QTreeWidget#channelTree {{ background: transparent; border: none; outline: none; }}
QPushButton#groupAdd {{ border: 1px solid transparent; background: transparent; padding: 0; color: {MUTED}; font-size: 20px; }}
QPushButton#groupAdd:hover {{ background: {SELECTION}; }}
QPushButton#groupAdd:focus {{ border-color: {FOCUS}; }}
"""

SLIM_SCROLLBAR_STYLE = f"""
QScrollBar:vertical {{ width: 9px; margin: 3px 2px 3px 0; background: transparent; border: none; }}
QScrollBar::handle:vertical {{ min-height: 28px; background: {BORDER}; border-radius: 3px; }}
QScrollBar::handle:vertical:hover {{ background: {DISABLED}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""
