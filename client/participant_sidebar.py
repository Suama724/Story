"""Channel navigation and status icons for the participant lobby."""

from PySide6.QtWidgets import QWidget

from .channel_sidebar import CONNECTED, DISCONNECTED, ICON_SIZE, ChannelSidebar
from .channel_tree import ChannelTree
from .ui_icons import DEFAULT_AVATAR_SIZE, default_avatar_icon, phosphor_icon, svg_icon


SERVER_ICON = b"""
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor"
     stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <rect x="3" y="4" width="18" height="6" rx="1.8"/>
  <rect x="3" y="14" width="18" height="6" rx="1.8"/>
  <path d="M7 7h.1m-.1 10h.1M13 7h4m-4 10h4"/>
</svg>
"""


class ParticipantSidebar(ChannelSidebar):
    def __init__(self, channels: ChannelTree, lobby: QWidget) -> None:
        super().__init__(channels, lobby)
        self._server_icons = {
            opened: svg_icon(SERVER_ICON, color, size=ICON_SIZE)
            for opened, color in ((True, CONNECTED), (False, DISCONNECTED))
        }
        self._refresh_icon = phosphor_icon("bold/arrows-clockwise-bold.svg", size=ICON_SIZE)
        self.server_status = self.action("刷新服务器状态")
        self.actions.addWidget(self.server_status)
        self.actions.addStretch()
        self.personal_button = self.action("个人", default_avatar_icon(), icon_size=DEFAULT_AVATAR_SIZE)
        self.actions.addWidget(self.personal_button)
        self.actions.addWidget(self.connection_button)

    def set_server_state(self, opened: bool | None, *, checking: bool) -> None:
        label = "正在检查服务器…" if checking else {
            True: "服务器已开启", False: "服务器未开启", None: "暂时无法确认服务器状态",
        }[opened]
        self.server_status.setIcon(self._refresh_icon if checking else self._server_icons[opened is True])
        self.server_status.setToolTip(label)
        self.server_status.setAccessibleDescription(label)

    def show_server_status(self) -> None:
        self._show_tooltip(self.server_status)
