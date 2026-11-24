"""登录页与参与者大厅共用的极简浅色样式。"""

STYLE = """
QWidget { color: #203548; font-size: 14px; }
QWidget#player { background: #e9eef3; }
QFrame#card { background: white; border: 1px solid #ccd6df; border-radius: 6px; }
QLabel#title { font-size: 24px; font-weight: 600; }
QLabel#muted { color: #687b8d; }
QLineEdit, QPlainTextEdit, QComboBox { background: white; border: 1px solid #ccd6df; border-radius: 4px; padding: 7px; selection-background-color: #d5e8f8; }
QLineEdit:focus, QPlainTextEdit:focus { border: 1px solid #326b97; }
QPushButton { background: #f6f8fa; border: 1px solid #bbcbd8; border-radius: 4px; padding: 8px 14px; }
QPushButton:hover { background: #e6eff7; }
QPushButton:focus { border: 2px solid #326b97; }
QPushButton#primary { background: #326b97; color: white; }
QPushButton:disabled, QPushButton#primary:disabled { background: #edf1f5; color: #95a3af; border-color: #d5dee6; }
"""
