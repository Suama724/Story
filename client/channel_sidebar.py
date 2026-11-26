"""Shared icon rail, channel navigation and hover tips for Story lobbies."""

from PySide6.QtCore import QEvent, QPoint, QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from .channel_tree import ChannelTree
from .ui_icons import DEFAULT_AVATAR_SIZE, default_avatar_icon, phosphor_icon
from .ui_style import FOCUS, HOVER, SELECTION, SLIM_SCROLLBAR_STYLE, SURFACE, TEXT


BUTTON_SIZE = 36
ICON_SIZE = 18
CONNECTED = "#2d7650"
DISCONNECTED = "#a0a0a0"
STYLE = f"""
QFrame#channelSidebarDivider {{ background: #dcdcdc; border: none; }}
QToolButton#sidebarAction, QLabel#sidebarAvatar {{
    background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 0;
}}
QToolButton#sidebarAction:hover, QLabel#sidebarAvatar:hover {{ background: {HOVER}; }}
QToolButton#sidebarAction:pressed {{ background: {SELECTION}; }}
QToolButton#sidebarAction:focus {{ border-color: {FOCUS}; }}
"""
TOOLTIP_STYLE = f"""
QLabel#sidebarTooltip {{
    color: {SURFACE}; background: {TEXT}; border: none; border-radius: 10px; padding: 4px 8px; font-size: 13px;
}}
"""


class ChannelSidebar(QFrame):
    def __init__(self, channels: ChannelTree, lobby: QWidget) -> None:
        super().__init__()
        self.setObjectName("channelSidebar")
        self.setStyleSheet(STYLE)
        self._connection_icons = {
            True: phosphor_icon("bold/plugs-connected-bold.svg", CONNECTED, size=ICON_SIZE),
            False: phosphor_icon("bold/plugs-bold.svg", DISCONNECTED, size=ICON_SIZE),
        }

        columns = QHBoxLayout(self)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(0)
        rail = QWidget()
        self.actions = QVBoxLayout(rail)
        self.actions.setContentsMargins(7, 14, 7, 14)
        self.actions.setSpacing(6)
        columns.addWidget(rail)

        divider = QFrame()
        divider.setObjectName("channelSidebarDivider")
        divider.setFixedWidth(1)
        separator = QVBoxLayout()
        separator.setContentsMargins(0, 12, 0, 12)
        separator.addWidget(divider)
        columns.addLayout(separator)

        self.navigation = QVBoxLayout()
        self.navigation.setContentsMargins(6, 12, 12, 12)
        channels.verticalScrollBar().setStyleSheet(SLIM_SCROLLBAR_STYLE)
        self.navigation.addWidget(channels, 1)
        columns.addLayout(self.navigation, 1)

        self._lobby = lobby
        self._tooltip_owner: QWidget | None = None
        self._tooltip = QLabel(lobby)
        self._tooltip.setObjectName("sidebarTooltip")
        self._tooltip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._tooltip.setTextFormat(Qt.TextFormat.PlainText)
        self._tooltip.setStyleSheet(TOOLTIP_STYLE)
        self._tooltip.hide()
        lobby.installEventFilter(self)
        self.connection_button = self.action()

    def action(self, label: str = "", icon: QIcon | None = None, *, icon_size: int = ICON_SIZE) -> QToolButton:
        button = QToolButton()
        button.setObjectName("sidebarAction")
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        button.setIconSize(QSize(icon_size, icon_size))
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setText(label)
        button.setToolTip(label)
        if icon is not None:
            button.setIcon(icon)
        button.installEventFilter(self)
        return button

    def avatar_placeholder(self) -> QLabel:
        label = QLabel()
        label.setObjectName("sidebarAvatar")
        label.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setPixmap(default_avatar_icon().pixmap(QSize(DEFAULT_AVATAR_SIZE, DEFAULT_AVATAR_SIZE), 2))
        label.setToolTip("个人")
        label.setAccessibleName("个人")
        label.installEventFilter(self)
        return label

    def set_connection_state(self, connected: bool, action: str, status: str) -> None:
        self.connection_button.setIcon(self._connection_icons[connected])
        self.connection_button.setText(action)
        self.connection_button.setToolTip(status)
        self.connection_button.setAccessibleDescription(status)

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if watched is self._lobby:
            if kind in (QEvent.Type.Hide, QEvent.Type.WindowDeactivate):
                self._hide_tooltip()
        elif kind == QEvent.Type.ToolTip:
            self._show_tooltip(watched)
            return True
        elif watched is self._tooltip_owner:
            if kind in (QEvent.Type.Leave, QEvent.Type.Hide, QEvent.Type.FocusOut, QEvent.Type.MouseButtonPress,
                        QEvent.Type.KeyPress):
                self._hide_tooltip()
            elif kind == QEvent.Type.ToolTipChange:
                self._show_tooltip(watched)
        return super().eventFilter(watched, event)

    def _show_tooltip(self, owner: QWidget) -> None:
        self._tooltip_owner = owner
        self._tooltip.setText(owner.toolTip())
        self._tooltip.adjustSize()
        anchor = owner.mapTo(self._lobby, QPoint(owner.width() + 8, owner.height() // 2))
        self._tooltip.move(anchor.x(), anchor.y() - self._tooltip.height() // 2)
        self._tooltip.show()
        self._tooltip.raise_()

    def _hide_tooltip(self) -> None:
        self._tooltip_owner = None
        self._tooltip.hide()
