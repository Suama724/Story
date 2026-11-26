"""Shared title and actions for a Story chat channel."""

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton

from .ui_icons import phosphor_icon
from .ui_style import BORDER, DISABLED, FOCUS, HOVER, MUTED, SELECTION, SURFACE, TEXT
from .ui_widgets import ElidedButton, ElidedLabel

ICON_SIZE = 20
STYLE = f"""
QLabel#channelTitle {{ color: {TEXT}; font-size: 16px; font-weight: 600; }}
QLabel#channelSeparator {{ color: {DISABLED}; font-size: 16px; font-weight: 600; }}
QPushButton#channelDescription, QToolButton#channelAction, QToolButton#archiveChannel {{
    background: transparent; border: 1px solid transparent; border-radius: 4px; padding: 0;
}}
QPushButton#channelDescription {{ color: {MUTED}; font-size: 14px; text-align: left; }}
QPushButton#channelDescription:hover {{ color: {TEXT}; }}
QPushButton#channelDescription:disabled {{ color: {MUTED}; }}
QToolButton#channelAction:hover {{ background: {HOVER}; }}
QToolButton#channelAction:pressed {{ background: {SELECTION}; }}
QToolButton#archiveChannel:hover {{ background: #faedef; }}
QToolButton#archiveChannel:pressed {{ background: #f5dce0; }}
QPushButton#channelDescription:focus, QToolButton#channelAction:focus, QToolButton#archiveChannel:focus {{
    border-color: {FOCUS};
}}
"""
DIVIDER_STYLE = f"QFrame#channelHeader {{ background: {SURFACE}; border-bottom: 1px solid {BORDER}; }}"


class ChannelHeader(QFrame):
    def __init__(self, *, can_archive: bool = False, divider: bool = True) -> None:
        super().__init__()
        self.setObjectName("channelHeader")
        self.setStyleSheet(STYLE + (DIVIDER_STYLE if divider else ""))
        self.setFixedHeight(48)

        top = QHBoxLayout(self)
        top.setContentsMargins(20, 0, 20, 0)
        top.setSpacing(8)
        self.channel_icon = QLabel()
        self.channel_icon.setPixmap(phosphor_icon("bold/hash-bold.svg").pixmap(QSize(ICON_SIZE, ICON_SIZE), 2))
        self.channel_icon.setFixedWidth(ICON_SIZE)
        top.addWidget(self.channel_icon, alignment=Qt.AlignmentFlag.AlignVCenter)

        self.title = ElidedLabel()
        self.title.setObjectName("channelTitle")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setMaximumWidth(176)
        top.addWidget(self.title)

        self.separator = QLabel("·")
        self.separator.setObjectName("channelSeparator")
        top.addWidget(self.separator)

        self.description_button = ElidedButton()
        self.description_button.setObjectName("channelDescription")
        self.description_button.setAccessibleName("频道简介")
        self.description_button.setToolTip("频道简介")
        self.description_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.description_button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.description_button.setMinimumWidth(48)
        self.description_button.setFixedHeight(32)
        top.addWidget(self.description_button, 1, Qt.AlignmentFlag.AlignLeft)

        actions = QHBoxLayout()
        actions.setSpacing(4)
        self.members_button = self._action("频道成员", phosphor_icon("fill/users-fill.svg"))
        self.history_button = self._action("聊天记录", phosphor_icon("bold/scroll-bold.svg"))
        actions.addWidget(self.members_button)
        actions.addWidget(self.history_button)
        if can_archive:
            actions.addSpacing(20)
            self.archive_button = self._action(
                "归档频道", phosphor_icon("bold/archive-bold.svg", "#b23743", "#cb9ba0"), "archiveChannel",
            )
            actions.addWidget(self.archive_button)
        top.addLayout(actions)
        self.set_channel()

    def set_channel(self, name: str = "", description: str = "") -> None:
        self.title.setText(name)
        self.title.setToolTip(name)
        self.separator.setVisible(bool(name))
        preview = " ".join((description or "").split()) or "暂无简介"
        self.description_button.setText(preview.replace("&", "&&") if name else "")
        self.description_button.setAccessibleDescription(preview if name else "")

    @staticmethod
    def _action(label: str, icon: QIcon, name: str = "channelAction") -> QToolButton:
        button = QToolButton()
        button.setObjectName(name)
        button.setText(label)
        button.setAccessibleName(label)
        button.setToolTip(label)
        button.setIcon(icon)
        button.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        button.setFixedSize(32, 32)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button
