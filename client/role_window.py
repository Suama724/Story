"""从一个桌面入口进入玩家端或组织者工作台。"""

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from .player_window import STYLE, PlayerWindow, label


class RoleWindow(QWidget):
    def __init__(self, data_root: Path) -> None:
        super().__init__()
        self.data_root = data_root
        self._window: QWidget | None = None
        self.setObjectName("player")
        self.setWindowTitle("Story · 选择身份")
        self.resize(650, 350)
        self.setMinimumSize(570, 330)
        self.setStyleSheet(STYLE + """
            QLabel#wordmark { color: #326b97; font: 600 19px 'Bahnschrift'; }
            QFrame#organizer { background: #dfe8ef; border: 1px solid #c0d0de; border-radius: 6px; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 26)
        layout.setSpacing(14)
        layout.addWidget(label("STORY", "wordmark"))
        layout.addWidget(label("今晚，以什么身份入场？", "title"))
        cards = QHBoxLayout()
        cards.setSpacing(14)
        cards.addWidget(self._card("玩家", "选择已用角色，或用角色名加入团。", self._open_player), 1)
        cards.addWidget(self._card("组织者", "管理团本、角色和主机监听。", self._open_organizer), 1)
        layout.addLayout(cards, 1)

    def _card(self, title: str, detail: str, opened: Callable[[], None]) -> QFrame:
        card = QFrame()
        card.setObjectName("card" if title == "玩家" else "organizer")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(19, 18, 19, 18)
        layout.addWidget(label(title, "title"))
        layout.addWidget(label(detail, "muted"))
        layout.addStretch()
        button = QPushButton(f"进入{title}端")
        if title == "玩家":
            button.setObjectName("primary")
        button.clicked.connect(opened)
        layout.addWidget(button)
        return card

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
