"""从一个桌面入口进入玩家端或组织者工作台。"""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from .player_window import PlayerWindow
from .ui_style import STYLE
from .window_title_bar import BAR_HEIGHT, window_layout


class RoleWindow(QWidget):
    def __init__(self, data_root: Path) -> None:
        super().__init__()
        self.data_root = data_root
        self._window: QWidget | None = None
        self.resize(420, 176 + BAR_HEIGHT)
        self.setMinimumSize(320, 160 + BAR_HEIGHT)
        self.setStyleSheet(STYLE)
        self.title_bar, layout = window_layout(self, self, "选择身份")
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        for title, opened in (
            ("作为参与者", self._open_player),
            ("作为组织者", self._open_organizer),
        ):
            button = QPushButton(title)
            button.setMinimumHeight(48)
            button.clicked.connect(opened)
            layout.addWidget(button, 1)

    def _open_player(self) -> None:
        self._show_window(PlayerWindow())

    def _open_organizer(self) -> None:
        from host.host_window import HostWindow

        self._show_window(HostWindow(self.data_root))

    def _show_window(self, window: QWidget) -> None:
        self._window = window
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        window.destroyed.connect(self._return)
        window.show()
        self.hide()

    def _return(self) -> None:
        self._window = None
        self.show()

    def closeEvent(self, event: QCloseEvent) -> None:
        event.accept()
        QApplication.quit()
