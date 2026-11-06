import sys

from PySide6.QtWidgets import QApplication

from .lobby_window import LobbyWindow


def main() -> int:
    app = QApplication(sys.argv)
    window = LobbyWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
